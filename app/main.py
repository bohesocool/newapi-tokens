#!/usr/bin/env python3
"""NewAPI Monitor — settings stored here, also persisted to SQLite."""
import os, json, sqlite3, subprocess, glob, shutil, secrets, hashlib, hmac, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from typing import Optional

# ── Config ──
SHANGHAI = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "monitor.db"
SNAPSHOT_DIR = BASE_DIR / "data" / "hourly_snapshots"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

# PostgreSQL access via docker exec
PG_CONTAINER = os.environ.get("PG_CONTAINER", "postgres")
PG_USER = os.environ.get("PG_USER", "root")
PG_DB = os.environ.get("PG_DB", "new-api")
TOKEN_NAME = os.environ.get("TOKEN_NAME", "ducker")
QUOTA_PER_USD = 500000

app = FastAPI(title="NewAPI Monitor")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Database ──
@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY,
            name TEXT DEFAULT '',
            rate REAL NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS daily_history (
            date TEXT PRIMARY KEY,
            total_real REAL,
            total_usd REAL,
            total_calls INTEGER,
            channels_json TEXT,
            report_text TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS rate_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            old_rate REAL,
            new_rate REAL NOT NULL,
            changed_at TEXT NOT NULL,
            notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        # Seed default channels if empty
        row = conn.execute("SELECT count(*) FROM channels").fetchone()
        if row[0] == 0:
            defaults = [
                (1, "刀015", 0.01),
                (2, "冰0095", 0.04),
                (3, "kedaya010", 0.03),
                (4, "madou012", 0.06),
                (5, "madou0065", 0.1),
            ]
            conn.executemany("INSERT INTO channels (id, name, rate) VALUES (?,?,?)", defaults)
            conn.commit()

init_db()

# ── Settings (key-value) ──
def get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()

# ── Password hashing (stdlib PBKDF2) ──
def hash_password(pw):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100_000)
    return f"{salt}${dk.hex()}"

def verify_password(pw, stored):
    if not stored or "$" not in stored:
        return False
    salt, h = stored.split("$", 1)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100_000)
    return hmac.compare_digest(dk.hex(), h)

def gen_api_key():
    return "mon_" + secrets.token_urlsafe(32)

def seed_settings():
    """Generate persistent secret/key/password on first run."""
    if not get_setting("secret_key"):
        set_setting("secret_key", secrets.token_hex(32))
    if not get_setting("api_key"):
        set_setting("api_key", gen_api_key())
    if not get_setting("admin_password"):
        initial = os.environ.get("ADMIN_PASSWORD", "admin")
        set_setting("admin_password", hash_password(initial))

seed_settings()

# Session cookie (signed). Secret persisted in DB so logins survive restarts.
app.add_middleware(
    SessionMiddleware,
    secret_key=get_setting("secret_key"),
    session_cookie="monitor_session",
    max_age=14 * 24 * 3600,
    same_site="lax",
)

# ── Auth ──
def is_authed_session(request: Request) -> bool:
    return request.session.get("authed") is True

def check_api_key(request: Request) -> bool:
    key = get_setting("api_key")
    if not key:
        return False
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer " and hmac.compare_digest(auth[7:].strip(), key):
        return True
    xk = request.headers.get("x-api-key", "").strip()
    return bool(xk) and hmac.compare_digest(xk, key)

def require_auth(request: Request):
    """Browser session OR API key. Protects data endpoints."""
    if is_authed_session(request) or check_api_key(request):
        return True
    raise HTTPException(401, "Unauthorized")

def require_session(request: Request):
    """Browser session only. Protects admin/settings endpoints."""
    if is_authed_session(request):
        return True
    raise HTTPException(401, "Unauthorized")

# Login rate limit (in-memory, per client IP)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECS = 300
_login_fails = {}  # ip -> list[float] of recent failure timestamps

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"

def backfill_today_snapshots():
    """On startup, backfill any missing hourly snapshots for today using rates in effect at each hour."""
    now = now_shanghai()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Only completed hours, NOT current hour
    for h in range(now.hour):
        h_start = today_start + timedelta(hours=h)
        h_end = h_start + timedelta(hours=1)
        f = SNAPSHOT_DIR / f"{h_start.strftime('%Y-%m-%d')}_{h:02d}.json"
        if f.exists():
            continue
        raw = query_pg(int(h_start.timestamp()), int(h_end.timestamp()))
        ch_data = parse_pg_rows(raw)
        # Use rates in effect at that hour, not current rates
        rates_at = get_rates_at(h_start)
        total_real = 0.0
        total_usd = 0.0
        total_calls = 0
        for ch_id, d in ch_data.items():
            rate = rates_at.get(ch_id, {}).get("rate", 0)
            d["real_cost"] = d["usd"] * rate
            total_real += d["real_cost"]
            total_usd += d["usd"]
            total_calls += d["calls"]
        save_hourly_snapshot(h_start, ch_data, total_real, total_usd, total_calls)
        print(f"[Backfill] {h_start.strftime('%Y-%m-%d_%H')}: real=${total_real:.2f}")

# ── Helpers ──
def fmt_money(v):
    return f"${v:,.2f}"

def fmt_num(n):
    n = int(n)
    if n >= 1e9: return f"{n/1e9:.2f}B"
    if n >= 1e6: return f"{n/1e6:.2f}M"
    if n >= 1e3: return f"{n/1e3:.1f}K"
    return str(n)

def query_pg(start_ts, end_ts):
    """Query NewAPI PostgreSQL for ducker token usage."""
    # token_name passed as a psql variable (:'tok') so it is safely quoted;
    # start/end are coerced to int, QUOTA_PER_USD is an internal constant.
    sql = """BEGIN READ ONLY;
SELECT
  channel_id,
  count(*),
  COALESCE(sum(quota), 0),
  round(COALESCE(sum(quota), 0)::numeric / {qpu}, 4),
  COALESCE(sum(prompt_tokens), 0),
  COALESCE(sum(completion_tokens), 0)
FROM logs
WHERE type = 2
  AND token_name = :'tok'
  AND created_at >= {start}
  AND created_at < {end}
GROUP BY channel_id
ORDER BY channel_id;
COMMIT;""".format(qpu=QUOTA_PER_USD, start=int(start_ts), end=int(end_ts))
    try:
        result = subprocess.run(
            ["/usr/bin/docker", "exec", PG_CONTAINER, "psql",
             "-U", PG_USER, "-d", PG_DB,
             "-t", "-A", "-F", "|", "-v", f"tok={TOKEN_NAME}", "-c", sql],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip()
    except Exception as e:
        return ""

def parse_pg_rows(raw):
    """Parse pipe-separated rows into channel dict."""
    channels = {}
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        ch_id = int(parts[0])
        channels[ch_id] = {
            "calls": int(parts[1]),
            "quota": int(parts[2]),
            "usd": float(parts[3]),
            "prompt_tokens": int(parts[4]),
            "completion_tokens": int(parts[5]),
        }
    return channels

def get_rates():
    """Get channel rates from DB."""
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, rate FROM channels ORDER BY id").fetchall()
        return {r["id"]: {"name": r["name"], "rate": r["rate"]} for r in rows}

def now_shanghai():
    return datetime.now(SHANGHAI)

def get_rate_at(channel_id, target_dt):
    """Get the rate for a channel at a specific point in time (Shanghai time).
    Checks rate_history for changes before target_dt, falls back to current rate."""
    target_str = target_dt.strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        row = conn.execute("""
            SELECT new_rate FROM rate_history
            WHERE channel_id = ? AND changed_at <= ?
            ORDER BY changed_at DESC LIMIT 1
        """, (channel_id, target_str)).fetchone()
        if row:
            return row["new_rate"]
        # No history, use current rate
        row2 = conn.execute("SELECT rate FROM channels WHERE id = ?", (channel_id,)).fetchone()
        return row2["rate"] if row2 else 0

def get_rates_at(target_dt):
    """Get all channel rates at a specific point in time."""
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, rate FROM channels").fetchall()
    return {r["id"]: {"name": r["name"], "rate": get_rate_at(r["id"], target_dt)} for r in rows}

# ── Snapshot helpers ──
def save_hourly_snapshot(start_dt, channels_data, total_real, total_usd, total_calls):
    snap = {
        "date": start_dt.strftime("%Y-%m-%d"),
        "hour": start_dt.hour,
        "channels": {},
        "total_real": total_real,
        "total_usd": total_usd,
        "total_calls": total_calls,
    }
    for ch_id, d in channels_data.items():
        snap["channels"][str(ch_id)] = {
            "calls": d["calls"],
            "usd": d["usd"],
            "real_cost": d["real_cost"],
            "prompt_tokens": d["prompt_tokens"],
            "completion_tokens": d["completion_tokens"],
        }
    f = SNAPSHOT_DIR / f"{start_dt.strftime('%Y-%m-%d')}_{start_dt.hour:02d}.json"
    f.write_text(json.dumps(snap, ensure_ascii=False, indent=2))
    return f

def load_snapshots(start_dt, end_dt):
    """Load all hourly snapshots between start and end (exclusive)."""
    channels = {}
    total_real = 0.0
    total_usd = 0.0
    total_calls = 0
    missing = []
    current = start_dt
    while current < end_dt:
        f = SNAPSHOT_DIR / f"{current.strftime('%Y-%m-%d')}_{current.hour:02d}.json"
        if f.exists():
            snap = json.loads(f.read_text())
            for ch_id_str, ch_data in snap.get("channels", {}).items():
                ch_id = int(ch_id_str)
                if ch_id not in channels:
                    channels[ch_id] = {"calls": 0, "usd": 0.0, "real_cost": 0.0,
                                      "prompt_tokens": 0, "completion_tokens": 0}
                channels[ch_id]["calls"] += ch_data["calls"]
                channels[ch_id]["usd"] += ch_data["usd"]
                channels[ch_id]["real_cost"] += ch_data["real_cost"]
                channels[ch_id]["prompt_tokens"] += ch_data["prompt_tokens"]
                channels[ch_id]["completion_tokens"] += ch_data["completion_tokens"]
            total_real += snap.get("total_real", 0)
            total_usd += snap.get("total_usd", 0)
            total_calls += snap.get("total_calls", 0)
        else:
            missing.append(current.strftime("%m-%d %H:00"))
        current += timedelta(hours=1)
    return channels, total_real, total_usd, total_calls, missing

def cleanup_daily_snapshots(date_str):
    """Delete snapshot files for a given date."""
    for f in glob.glob(str(SNAPSHOT_DIR / f"{date_str}_*.json")):
        os.remove(f)

def save_daily_history(date_str, channels, total_real, total_usd, total_calls):
    """Save daily report to SQLite history."""
    channels_json = json.dumps(
        {str(k): v for k, v in channels.items()},
        ensure_ascii=False
    )
    with get_db() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO daily_history (date, total_real, total_usd, total_calls, channels_json)
        VALUES (?, ?, ?, ?, ?)
        """, (date_str, total_real, total_usd, total_calls, channels_json))
        conn.commit()

# Run backfill after all helpers are defined
backfill_today_snapshots()

# ── Models ──
class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    rate: Optional[float] = None

class ChannelCreate(BaseModel):
    id: int
    name: str = ""
    rate: float = 0.0

# ── API Routes ──

@app.get("/api/channels", dependencies=[Depends(require_auth)])
def api_get_channels():
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, rate, updated_at FROM channels ORDER BY id").fetchall()
        return [dict(r) for r in rows]

@app.put("/api/channels/{ch_id}", dependencies=[Depends(require_auth)])
def api_update_channel(ch_id: int, body: ChannelUpdate):
    with get_db() as conn:
        row = conn.execute("SELECT id FROM channels WHERE id = ?", (ch_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Channel not found")
        updates = []
        params = []
        if body.name is not None:
            updates.append("name = ?")
            params.append(body.name)
        if body.rate is not None:
            updates.append("rate = ?")
            params.append(body.rate)
        if updates:
            updates.append("updated_at = datetime('now')")
            params.append(ch_id)
            conn.execute(f"UPDATE channels SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
    return {"ok": True}

@app.post("/api/channels", dependencies=[Depends(require_auth)])
def api_create_channel(body: ChannelCreate):
    with get_db() as conn:
        try:
            conn.execute("INSERT INTO channels (id, name, rate) VALUES (?, ?, ?)",
                        (body.id, body.name, body.rate))
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Channel already exists")
    return {"ok": True}

@app.delete("/api/channels/{ch_id}", dependencies=[Depends(require_auth)])
def api_delete_channel(ch_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM channels WHERE id = ?", (ch_id,))
        conn.commit()
    return {"ok": True}

@app.get("/api/hourly", dependencies=[Depends(require_auth)])
def api_hourly():
    """Get current hour data (live query) + save snapshot for previous completed hour."""
    now = now_shanghai()
    
    # Current hour (in-progress)
    cur_start = now.replace(minute=0, second=0, microsecond=0)
    cur_end = now
    raw = query_pg(int(cur_start.timestamp()), int(cur_end.timestamp()))
    cur_channels = parse_pg_rows(raw)
    
    rates = get_rates()
    cur_data = {}
    cur_total_real = 0.0
    cur_total_usd = 0.0
    cur_total_calls = 0
    for ch_id, d in cur_channels.items():
        rate = rates.get(ch_id, {}).get("rate", 0)
        real_cost = d["usd"] * rate
        cur_data[ch_id] = {
            **d,
            "real_cost": real_cost,
            "rate": rate,
            "name": rates.get(ch_id, {}).get("name", ""),
        }
        cur_total_real += real_cost
        cur_total_usd += d["usd"]
        cur_total_calls += d["calls"]
    
    # Today's accumulated (from snapshots + current hour)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    snap_channels, snap_real, snap_usd, snap_calls, missing = load_snapshots(today_start, cur_start)
    
    # Merge snapshot + current hour
    all_channels = {}
    for ch_id, d in snap_channels.items():
        all_channels[ch_id] = {**d, "name": rates.get(ch_id, {}).get("name", "")}
    for ch_id, d in cur_data.items():
        if ch_id not in all_channels:
            all_channels[ch_id] = {"calls": 0, "usd": 0, "real_cost": 0,
                                   "prompt_tokens": 0, "completion_tokens": 0,
                                   "name": d.get("name", "")}
        all_channels[ch_id]["calls"] += d["calls"]
        all_channels[ch_id]["usd"] += d["usd"]
        all_channels[ch_id]["real_cost"] += d["real_cost"]
        all_channels[ch_id]["prompt_tokens"] += d["prompt_tokens"]
        all_channels[ch_id]["completion_tokens"] += d["completion_tokens"]
    
    today_total_real = snap_real + cur_total_real
    today_total_usd = snap_usd + cur_total_usd
    today_total_calls = snap_calls + cur_total_calls
    
    # Hourly breakdown for today (from snapshots)
    hourly = []
    for h in range(24):
        h_dt = today_start + timedelta(hours=h)
        if h_dt > now:
            break
        f = SNAPSHOT_DIR / f"{h_dt.strftime('%Y-%m-%d')}_{h:02d}.json"
        if f.exists():
            snap = json.loads(f.read_text())
            hourly.append({
                "hour": h,
                "real_cost": snap.get("total_real", 0),
                "usd": snap.get("total_usd", 0),
                "calls": snap.get("total_calls", 0),
            })
        elif h_dt < cur_start:
            hourly.append({"hour": h, "real_cost": 0, "usd": 0, "calls": 0})
    
    # Add current hour
    hourly.append({
        "hour": now.hour,
        "real_cost": cur_total_real,
        "usd": cur_total_usd,
        "calls": cur_total_calls,
    })
    
    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "today_date": now.strftime("%Y-%m-%d"),
        "current_hour": {
            "start": cur_start.strftime("%H:00"),
            "end": now.strftime("%H:%M"),
            "channels": {str(k): v for k, v in cur_data.items()},
            "total_real": cur_total_real,
            "total_usd": cur_total_usd,
            "total_calls": cur_total_calls,
        },
        "today_total": {
            "total_real": today_total_real,
            "total_usd": today_total_usd,
            "total_calls": today_total_calls,
        },
        "channels": {str(k): v for k, v in all_channels.items()},
        "hourly": hourly,
        "rates": {str(k): v for k, v in rates.items()},
    }

@app.post("/api/snapshot/hourly", dependencies=[Depends(require_auth)])
def api_snapshot_hourly():
    """Save snapshot for the previous completed hour. Called by internal scheduler."""
    now = now_shanghai()
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    end = start + timedelta(hours=1)
    raw = query_pg(int(start.timestamp()), int(end.timestamp()))
    ch_data = parse_pg_rows(raw)
    # Use rates in effect at that hour
    rates_at = get_rates_at(start)
    total_real = 0.0
    total_usd = 0.0
    total_calls = 0
    for ch_id, d in ch_data.items():
        rate = rates_at.get(ch_id, {}).get("rate", 0)
        d["real_cost"] = d["usd"] * rate
        total_real += d["real_cost"]
        total_usd += d["usd"]
        total_calls += d["calls"]
    f = save_hourly_snapshot(start, ch_data, total_real, total_usd, total_calls)
    return {"ok": True, "snapshot": str(f), "total_real": total_real}

@app.get("/api/daily/{date_str}", dependencies=[Depends(require_auth)])
def api_daily(date_str: str):
    """Get daily report from snapshots or history."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    except ValueError:
        raise HTTPException(400, "Invalid date format, use YYYY-MM-DD")
    
    start = dt
    end = dt + timedelta(days=1)
    
    channels, total_real, total_usd, total_calls, missing = load_snapshots(start, end)
    
    if not channels:
        # Try history
        with get_db() as conn:
            row = conn.execute("SELECT * FROM daily_history WHERE date = ?", (date_str,)).fetchone()
        if row:
            channels = json.loads(row["channels_json"]) if row["channels_json"] else {}
            total_real = row["total_real"]
            total_usd = row["total_usd"]
            total_calls = row["total_calls"]
            missing = []
    
    rates = get_rates()
    for ch_id_str, d in channels.items():
        ch_id = int(ch_id_str)
        d["name"] = rates.get(ch_id, {}).get("name", "")
    
    return {
        "date": date_str,
        "channels": {str(k): v for k, v in channels.items()},
        "total_real": total_real,
        "total_usd": total_usd,
        "total_calls": total_calls,
        "missing": missing,
    }

@app.post("/api/finalize-daily", dependencies=[Depends(require_auth)])
def api_finalize_daily():
    """Save today's data to history and cleanup snapshots. Called by cron before daily report."""
    now = now_shanghai()
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    start = datetime.strptime(yesterday, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    end = start + timedelta(days=1)
    
    channels, total_real, total_usd, total_calls, missing = load_snapshots(start, end)
    save_daily_history(yesterday, channels, total_real, total_usd, total_calls)
    cleanup_daily_snapshots(yesterday)
    
    return {"ok": True, "date": yesterday, "total_real": total_real, "missing": missing}

@app.get("/api/history", dependencies=[Depends(require_auth)])
def api_history():
    """Get list of saved daily history."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT date, total_real, total_usd, total_calls FROM daily_history ORDER BY date DESC LIMIT 30"
        ).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/snapshot/{date_str}/{hour}", dependencies=[Depends(require_auth)])
def api_get_snapshot(date_str: str, hour: int):
    """Return one completed hour's snapshot (used by the report hook)."""
    f = SNAPSHOT_DIR / f"{date_str}_{hour:02d}.json"
    if not f.exists():
        raise HTTPException(404, "Snapshot not found")
    return json.loads(f.read_text(encoding="utf-8"))

@app.get("/api/health")
def api_health():
    return {"status": "ok", "time": now_shanghai().strftime("%Y-%m-%d %H:%M:%S")}

# ── Auth & Settings ──
class LoginBody(BaseModel):
    password: str

class PasswordBody(BaseModel):
    old_password: str
    new_password: str

@app.post("/api/login")
def api_login(body: LoginBody, request: Request):
    ip = _client_ip(request)
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_LOCKOUT_SECS]
    if len(fails) >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(429, f"尝试次数过多，请 {LOGIN_LOCKOUT_SECS // 60} 分钟后再试")
    if not verify_password(body.password, get_setting("admin_password")):
        fails.append(now)
        _login_fails[ip] = fails
        raise HTTPException(401, "密码错误")
    _login_fails.pop(ip, None)  # reset on success
    request.session["authed"] = True
    return {"ok": True}

@app.post("/api/logout")
def api_logout(request: Request):
    request.session.clear()
    return {"ok": True}

@app.get("/api/me")
def api_me(request: Request):
    return {"authed": is_authed_session(request)}

@app.get("/api/settings", dependencies=[Depends(require_session)])
def api_get_settings():
    return {"api_key": get_setting("api_key")}

@app.post("/api/settings/regenerate-key", dependencies=[Depends(require_session)])
def api_regenerate_key():
    new_key = gen_api_key()
    set_setting("api_key", new_key)
    return {"api_key": new_key}

@app.post("/api/settings/password", dependencies=[Depends(require_session)])
def api_change_password(body: PasswordBody):
    if not verify_password(body.old_password, get_setting("admin_password")):
        raise HTTPException(401, "原密码错误")
    if len(body.new_password) < 4:
        raise HTTPException(400, "新密码至少 4 位")
    set_setting("admin_password", hash_password(body.new_password))
    return {"ok": True}

# ── Static files & main page ──
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if is_authed_session(request):
        return RedirectResponse("/")
    return (BASE_DIR / "templates" / "login.html").read_text(encoding="utf-8")

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not is_authed_session(request):
        return RedirectResponse("/login")
    return (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")

if __name__ == "__main__":
    import uvicorn
    import threading, time as _time

    def hourly_scheduler():
        """Auto-save snapshot at every full hour."""
        while True:
            now = now_shanghai()
            # Sleep until next full hour + 30 seconds
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            wait_secs = (next_hour - now).total_seconds() + 30
            _time.sleep(max(1, wait_secs))
            try:
                # Call the handler directly — no HTTP, so no auth needed.
                result = api_snapshot_hourly()
                print(f"[Scheduler] Snapshot saved at {now_shanghai()}: ${result.get('total_real', 0):.2f}")
            except Exception as e:
                print(f"[Scheduler] Snapshot failed: {e}")

    t = threading.Thread(target=hourly_scheduler, daemon=True)
    t.start()

    uvicorn.run(app, host="0.0.0.0", port=9217)