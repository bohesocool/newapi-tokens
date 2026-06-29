#!/usr/bin/env python3
"""NewAPI Monitor — settings stored here, also persisted to SQLite."""
import os, json, sqlite3, glob, secrets, hashlib, hmac, time, urllib.request, urllib.error, http.cookiejar
import psutil
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
from psycopg_pool import ConnectionPool

# ── Config ──
SHANGHAI = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "monitor.db"
SNAPSHOT_DIR = BASE_DIR / "data" / "hourly_snapshots"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

# PostgreSQL direct access via psycopg (read-only pool over the docker network).
PG_HOST = os.environ.get("PG_HOST", "postgres")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_USER = os.environ.get("PG_USER", "root")
PG_DB = os.environ.get("PG_DB", "new-api")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
TOKEN_NAME = os.environ.get("TOKEN_NAME", "ducker")
QUOTA_PER_USD = 500000

PG_CONNINFO = (
    f"host={PG_HOST} port={PG_PORT} user={PG_USER} dbname={PG_DB} "
    f"password={PG_PASSWORD} connect_timeout=10"
)
# Pool opened at startup. Every session is forced read-only + statement_timeout at
# the SERVER level, so the monitor can never write to the NewAPI database.
_pg_pool = ConnectionPool(
    PG_CONNINFO,
    min_size=1,
    max_size=4,
    open=False,
    kwargs={"options": "-c default_transaction_read_only=on -c statement_timeout=30000"},
)

def _pg_rows(sql, params=()):
    """Run a read-only SELECT via the pool. Returns list of row tuples, or None on failure."""
    try:
        with _pg_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    except Exception as e:
        print(f"[pg] query failed: {e}", flush=True)
        return None

# Short-TTL cache so concurrent dashboard polls collapse into one PG query.
DASH_CACHE_TTL = 10  # seconds
_resp_cache = {}  # key -> (expires_ts, value)

def cached_response(key, ttl, producer):
    hit = _resp_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    val = producer()
    _resp_cache[key] = (time.time() + ttl, val)
    return val

# 渠道错误率监控（可按需调整）
ERROR_RATE_THRESHOLD = 0.10   # 错误率 ≥ 10% 触发告警
ERROR_MIN_SAMPLE = 20         # 该分钟总调用 < 20 不参与判定，避免低流量误报

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
        CREATE TABLE IF NOT EXISTS cost_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,          -- 'income' or 'expense'
            amount REAL NOT NULL,
            date TEXT NOT NULL,          -- 'YYYY-MM-DD'
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)
        # Balance-check columns (added incrementally; ignore if already present).
        # bal_type: '' | 'sub2api' | 'newapi'; credentials stored in plaintext (self-hosted tool).
        for col, decl in [
            ("bal_type", "TEXT DEFAULT ''"),
            ("bal_url", "TEXT DEFAULT ''"),
            ("bal_account", "TEXT DEFAULT ''"),
            ("bal_password", "TEXT DEFAULT ''"),
            ("bal_rt", "TEXT DEFAULT ''"),
            ("bal_value", "REAL"),
            ("bal_checked_at", "TEXT DEFAULT ''"),
            ("bal_error", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE channels ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # column already exists
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
        if raw is None:
            print(f"[Backfill] skip {h_start.strftime('%Y-%m-%d_%H')}: pg query failed", flush=True)
            continue
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
    """Per-channel success(type=2) aggregates plus error(type=5) count for the token in
    [start, end). Read-only single query — uses idx_created_at_type. Returns rows or None.
    token_name/start/end are passed as real query parameters (no string interpolation)."""
    sql = """
SELECT
  channel_id,
  count(*) FILTER (WHERE type = 2),
  COALESCE(sum(quota) FILTER (WHERE type = 2), 0),
  round(COALESCE(sum(quota) FILTER (WHERE type = 2), 0)::numeric / %s, 4),
  COALESCE(sum(prompt_tokens) FILTER (WHERE type = 2), 0),
  COALESCE(sum(completion_tokens) FILTER (WHERE type = 2), 0),
  count(*) FILTER (WHERE type = 5)
FROM logs
WHERE type IN (2, 5)
  AND token_name = %s
  AND created_at >= %s
  AND created_at < %s
GROUP BY channel_id
ORDER BY channel_id"""
    return _pg_rows(sql, (QUOTA_PER_USD, TOKEN_NAME, int(start_ts), int(end_ts)))

def parse_pg_rows(rows):
    """Parse query_pg result rows into a channel dict."""
    channels = {}
    if not rows:
        return channels
    for r in rows:
        ch_id = int(r[0])
        channels[ch_id] = {
            "calls": int(r[1]),
            "quota": int(r[2]),
            "usd": float(r[3]),
            "prompt_tokens": int(r[4]),
            "completion_tokens": int(r[5]),
            "errors": int(r[6]),
        }
    return channels

def query_pg_error_rates(start_ts, end_ts):
    """Per-channel success(type=2)/error(type=5) counts for the token in [start, end).
    Read-only single aggregated query — uses idx_created_at_type. Returns dict or None."""
    sql = """
SELECT
  channel_id,
  count(*) FILTER (WHERE type = 2),
  count(*) FILTER (WHERE type = 5)
FROM logs
WHERE type IN (2, 5)
  AND token_name = %s
  AND created_at >= %s
  AND created_at < %s
GROUP BY channel_id
ORDER BY channel_id"""
    rows = _pg_rows(sql, (TOKEN_NAME, int(start_ts), int(end_ts)))
    if rows is None:
        return None
    return {int(r[0]): {"success": int(r[1]), "errors": int(r[2])} for r in rows}

def query_pg_minute_status(start_ts, end_ts):
    """Per-channel, per-minute success(type=2)/error(type=5) counts for the token in
    [start, end). Groups by channel_id and the minute bucket (created_at/60), so a single
    indexed read-only query yields one row per (channel, minute). Returns dict or None:
        {channel_id: {minute_bucket: {"success": n, "errors": n}}}"""
    sql = """
SELECT
  channel_id,
  created_at / 60 AS m,
  count(*) FILTER (WHERE type = 2),
  count(*) FILTER (WHERE type = 5)
FROM logs
WHERE type IN (2, 5)
  AND token_name = %s
  AND created_at >= %s
  AND created_at < %s
GROUP BY channel_id, m
ORDER BY channel_id, m"""
    rows = _pg_rows(sql, (TOKEN_NAME, int(start_ts), int(end_ts)))
    if rows is None:
        return None
    out = {}
    for r in rows:
        out.setdefault(int(r[0]), {})[int(r[1])] = {
            "success": int(r[2]), "errors": int(r[3]),
        }
    return out

def query_pg_rpm(start_ts, end_ts):
    """Per-channel request count (success type=2 + error type=5) for the token in
    [start, end). Used for a trailing-60s RPM gauge. Returns {channel_id: count} or None."""
    sql = """
SELECT channel_id, count(*)
FROM logs
WHERE type IN (2, 5)
  AND token_name = %s
  AND created_at >= %s
  AND created_at < %s
GROUP BY channel_id"""
    rows = _pg_rows(sql, (TOKEN_NAME, int(start_ts), int(end_ts)))
    if rows is None:
        return None
    return {int(r[0]): int(r[1]) for r in rows}

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

def get_rates():
    """Get channel rates from DB."""
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, rate FROM channels ORDER BY id").fetchall()
        return {r["id"]: {"name": r["name"], "rate": r["rate"]} for r in rows}

# ── 渠道余额查询（上游 sub2api / newapi）──
# 凭据以明文存于 channels 表（自托管个人工具）。两种上游最终都换算为 USD 余额。
BALANCE_HTTP_TIMEOUT = 15

def _api_err(body, status, prefix):
    """Build a readable error message from an upstream JSON body / status."""
    msg = ""
    if isinstance(body, dict):
        msg = body.get("message") or body.get("msg") or body.get("error") or ""
    return f"{prefix}: {msg or ('HTTP ' + str(status))}"

def _http_request(method, url, headers=None, body=None, opener=None):
    """HTTP request returning (status, parsed_json_or_text). body dict -> JSON.
    HTTPError is captured (so 4xx bodies are still parsed); network errors raise."""
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    h.setdefault("User-Agent", "newapi-monitor/1.0")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    o = opener or urllib.request.build_opener()
    try:
        resp = o.open(req, timeout=BALANCE_HTTP_TIMEOUT)
        raw, status = resp.read().decode("utf-8", "replace"), resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode("utf-8", "replace"), e.code
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = raw
    return status, parsed

def _fetch_sub2api(base, account, password, rt):
    """Return (balance_usd, new_rt). Prefers RT (refresh -> AT -> /auth/me);
    falls back to email+password login (which already returns balance)."""
    base = base.rstrip("/")
    api = base if base.endswith("/api/v1") else base + "/api/v1"
    new_rt, access = rt, None
    if rt:
        st, body = _http_request("POST", api + "/auth/refresh", body={"refresh_token": rt})
        if st == 200 and isinstance(body, dict) and body.get("code") == 0:
            d = body.get("data") or {}
            access = d.get("access_token")
            new_rt = d.get("refresh_token") or rt
        else:
            raise RuntimeError(_api_err(body, st, "RT 刷新失败"))
    elif account and password:
        st, body = _http_request("POST", api + "/auth/login",
                                 body={"email": account, "password": password, "turnstile_token": ""})
        if st == 200 and isinstance(body, dict) and body.get("code") == 0:
            d = body.get("data") or {}
            if d.get("requires_2fa"):
                raise RuntimeError("账号开启了 2FA，请改用 RT 方式")
            new_rt = d.get("refresh_token") or rt
            user = d.get("user") or {}
            if user.get("balance") is not None:  # login already returns balance
                return float(user["balance"]), new_rt
            access = d.get("access_token")
        else:
            raise RuntimeError(_api_err(body, st, "登录失败"))
    else:
        raise RuntimeError("未配置 RT 或账号密码")
    if not access:
        raise RuntimeError("未获取到 access_token")
    st, body = _http_request("GET", api + "/auth/me", headers={"Authorization": "Bearer " + access})
    if st == 200 and isinstance(body, dict) and body.get("code") == 0:
        d = body.get("data") or {}
        if d.get("balance") is not None:
            return float(d["balance"]), new_rt
        raise RuntimeError("返回中无 balance 字段")
    raise RuntimeError(_api_err(body, st, "获取余额失败"))

def _fetch_newapi(base, account, password):
    """Return balance_usd. Login (new-api-user:-1) -> session cookie + user id ->
    /api/user/self -> quota; balance = quota / QUOTA_PER_USD (= quota * 0.000002)."""
    base = base.rstrip("/")
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    st, body = _http_request("POST", base + "/api/user/login?turnstile=",
                             headers={"new-api-user": "-1"},
                             body={"username": account, "password": password}, opener=opener)
    if not (st == 200 and isinstance(body, dict) and body.get("success")):
        raise RuntimeError(_api_err(body, st, "登录失败"))
    uid = (body.get("data") or {}).get("id")
    if uid is None:
        raise RuntimeError("登录返回中无用户 id")
    st, body = _http_request("GET", base + "/api/user/self",
                             headers={"new-api-user": str(uid)}, opener=opener)
    if not (st == 200 and isinstance(body, dict) and body.get("success")):
        raise RuntimeError(_api_err(body, st, "获取额度失败"))
    quota = (body.get("data") or {}).get("quota")
    if quota is None:
        raise RuntimeError("返回中无 quota 字段")
    return float(quota) / QUOTA_PER_USD

def fetch_balance(cfg):
    """cfg: dict-like with bal_type/bal_url/bal_account/bal_password/bal_rt.
    Returns (value, error, new_rt): on success error is None; on failure value is None."""
    t = (cfg.get("bal_type") or "").strip()
    url = (cfg.get("bal_url") or "").strip()
    rt = cfg.get("bal_rt") or ""
    if not t or not url:
        return None, "未配置", rt
    try:
        if t == "sub2api":
            val, new_rt = _fetch_sub2api(url, cfg.get("bal_account") or "",
                                         cfg.get("bal_password") or "", rt)
            return val, None, new_rt
        if t == "newapi":
            return _fetch_newapi(url, cfg.get("bal_account") or "", cfg.get("bal_password") or ""), None, rt
        return None, f"未知类型: {t}", rt
    except Exception as e:
        return None, str(e), rt

def _save_balance_result(ch_id, value, error, new_rt):
    """Persist a fetch result to the channel row. Returns the check timestamp string."""
    now = now_shanghai().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "UPDATE channels SET bal_value = ?, bal_error = ?, bal_checked_at = ?, bal_rt = ? WHERE id = ?",
            (value, error or "", now, new_rt or "", ch_id),
        )
        conn.commit()
    return now

def refresh_channel_balance(ch_id):
    """Live-fetch one channel's balance and persist the result. Returns a result dict or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, bal_type, bal_url, bal_account, bal_password, bal_rt FROM channels WHERE id = ?",
            (ch_id,),
        ).fetchone()
    if not row:
        return None
    value, error, new_rt = fetch_balance(dict(row))
    checked = _save_balance_result(ch_id, value, error, new_rt)
    return {"id": ch_id, "value": value, "error": error or None, "checked_at": checked}

def refresh_all_balances():
    """Refresh every channel that has balance-checking configured. Returns list of results."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM channels WHERE COALESCE(bal_type,'') != '' AND COALESCE(bal_url,'') != ''"
        ).fetchall()
    results = []
    for r in rows:
        try:
            res = refresh_channel_balance(r["id"])
            if res:
                results.append(res)
        except Exception as e:
            print(f"[balance] channel {r['id']} refresh error: {e}", flush=True)
    return results


# ── Snapshot helpers ──
def save_hourly_snapshot(start_dt, channels_data, total_real, total_usd, total_calls):
    snap = {
        "date": start_dt.strftime("%Y-%m-%d"),
        "hour": start_dt.hour,
        "channels": {},
        "total_real": total_real,
        "total_usd": total_usd,
        "total_calls": total_calls,
        "total_errors": sum(d.get("errors", 0) for d in channels_data.values()),
    }
    for ch_id, d in channels_data.items():
        snap["channels"][str(ch_id)] = {
            "calls": d["calls"],
            "usd": d["usd"],
            "real_cost": d["real_cost"],
            "prompt_tokens": d["prompt_tokens"],
            "completion_tokens": d["completion_tokens"],
            "errors": d.get("errors", 0),
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
                                      "prompt_tokens": 0, "completion_tokens": 0, "errors": 0}
                channels[ch_id]["calls"] += ch_data["calls"]
                channels[ch_id]["usd"] += ch_data["usd"]
                channels[ch_id]["real_cost"] += ch_data["real_cost"]
                channels[ch_id]["prompt_tokens"] += ch_data["prompt_tokens"]
                channels[ch_id]["completion_tokens"] += ch_data["completion_tokens"]
                channels[ch_id]["errors"] += ch_data.get("errors", 0)
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

def cleanup_old_snapshots(keep_days=7):
    """Delete snapshot files whose date is older than keep_days (kept data lives in daily_history)."""
    cutoff = (now_shanghai() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    for f in glob.glob(str(SNAPSHOT_DIR / "*.json")):
        date_part = os.path.basename(f)[:10]  # YYYY-MM-DD
        if date_part < cutoff:
            try:
                os.remove(f)
            except OSError:
                pass

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

# Backfill + scheduler are started from the FastAPI startup event (see bottom of file),
# so they run under `uvicorn main:app` too — not only when executed as __main__.

# ── Models ──
class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    rate: Optional[float] = None

class ChannelCreate(BaseModel):
    id: int
    name: str = ""
    rate: float = 0.0

class BalanceConfigBody(BaseModel):
    bal_type: str = ""          # '' | 'sub2api' | 'newapi'
    bal_url: str = ""
    bal_account: str = ""
    bal_password: str = ""
    bal_rt: str = ""

class CostRecordBody(BaseModel):
    type: str           # 'income' or 'expense'
    amount: float
    date: str           # 'YYYY-MM-DD'
    note: str = ""

# ── API Routes ──

@app.get("/api/channels", dependencies=[Depends(require_auth)])
def api_get_channels():
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, rate, updated_at FROM channels ORDER BY id").fetchall()
        return [dict(r) for r in rows]

@app.put("/api/channels/{ch_id}", dependencies=[Depends(require_auth)])
def api_update_channel(ch_id: int, body: ChannelUpdate):
    with get_db() as conn:
        row = conn.execute("SELECT id, rate FROM channels WHERE id = ?", (ch_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Channel not found")
        old_rate = row["rate"]
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
            if body.rate is not None and body.rate != old_rate:
                conn.execute(
                    "INSERT INTO rate_history (channel_id, old_rate, new_rate, changed_at) VALUES (?, ?, ?, ?)",
                    (ch_id, old_rate, body.rate, now_shanghai().strftime("%Y-%m-%d %H:%M:%S")),
                )
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

# ── 渠道余额：配置 + 刷新 ──
@app.get("/api/channels/{ch_id}/balance-config", dependencies=[Depends(require_session)])
def api_get_balance_config(ch_id: int):
    """Return one channel's balance-check config (admin only — includes credentials)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT bal_type, bal_url, bal_account, bal_password, bal_rt FROM channels WHERE id = ?",
            (ch_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Channel not found")
    return dict(row)

@app.put("/api/channels/{ch_id}/balance-config", dependencies=[Depends(require_session)])
def api_set_balance_config(ch_id: int, body: BalanceConfigBody):
    t = body.bal_type.strip()
    if t not in ("", "sub2api", "newapi"):
        raise HTTPException(400, "类型必须是 sub2api 或 newapi")
    with get_db() as conn:
        row = conn.execute("SELECT id FROM channels WHERE id = ?", (ch_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Channel not found")
        conn.execute(
            "UPDATE channels SET bal_type = ?, bal_url = ?, bal_account = ?, "
            "bal_password = ?, bal_rt = ? WHERE id = ?",
            (t, body.bal_url.strip(), body.bal_account.strip(),
             body.bal_password, body.bal_rt.strip(), ch_id),
        )
        conn.commit()
    return {"ok": True}

@app.post("/api/channels/{ch_id}/balance/refresh", dependencies=[Depends(require_session)])
def api_refresh_channel_balance(ch_id: int):
    """Live-fetch one channel's balance (login/refresh upstream) and cache the result."""
    res = refresh_channel_balance(ch_id)
    if res is None:
        raise HTTPException(404, "Channel not found")
    return res

@app.get("/api/channels/balances", dependencies=[Depends(require_auth)])
def api_get_balances():
    """Cached balances for the dashboard. Credentials are NOT included."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, bal_type, bal_value, bal_checked_at, bal_error FROM channels ORDER BY id"
        ).fetchall()
    out = {}
    for r in rows:
        out[str(r["id"])] = {
            "type": r["bal_type"] or "",
            "configured": bool(r["bal_type"]),
            "value": r["bal_value"],
            "checked_at": r["bal_checked_at"] or "",
            "error": r["bal_error"] or "",
        }
    return out

@app.post("/api/channels/balances/refresh", dependencies=[Depends(require_session)])
def api_refresh_all_balances():
    """Refresh all configured channels' balances now."""
    return {"ok": True, "results": refresh_all_balances()}

@app.get("/api/hourly", dependencies=[Depends(require_auth)])
def api_hourly():
    """Get current hour data (live query). Cached briefly so concurrent polls share one query."""
    return cached_response("hourly", DASH_CACHE_TTL, _compute_hourly)

def _compute_hourly():
    now = now_shanghai()
    
    # Current hour (in-progress)
    cur_start = now.replace(minute=0, second=0, microsecond=0)
    cur_end = now
    raw = query_pg(int(cur_start.timestamp()), int(cur_end.timestamp()))
    pg_error = raw is None
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
                                   "prompt_tokens": 0, "completion_tokens": 0, "errors": 0,
                                   "name": d.get("name", "")}
        all_channels[ch_id]["calls"] += d["calls"]
        all_channels[ch_id]["usd"] += d["usd"]
        all_channels[ch_id]["real_cost"] += d["real_cost"]
        all_channels[ch_id]["prompt_tokens"] += d["prompt_tokens"]
        all_channels[ch_id]["completion_tokens"] += d["completion_tokens"]
        all_channels[ch_id]["errors"] = all_channels[ch_id].get("errors", 0) + d.get("errors", 0)
    
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
        "today_minutes": now.hour * 60 + now.minute,
        "error": "数据库查询失败，当前小时数据可能不准确" if pg_error else None,
    }

@app.get("/api/channel-status", dependencies=[Depends(require_auth)])
def api_channel_status(minutes: int = 60):
    """Per-channel, per-minute success-rate strip for the last `minutes` minutes.
    The newest cell is the in-progress current minute. Each cell carries the minute's
    success rate, or rate=None when that minute had no requests (rendered transparent)."""
    minutes = max(5, min(minutes, 240))
    return cached_response(f"chstatus:{minutes}", DASH_CACHE_TTL,
                           lambda: _compute_channel_status(minutes))

def _compute_channel_status(minutes):
    now = now_shanghai()
    cur_min = now.replace(second=0, microsecond=0)
    first_min = cur_min - timedelta(minutes=minutes - 1)
    start_ts = int(first_min.timestamp())
    end_ts = int((cur_min + timedelta(minutes=1)).timestamp())  # exclusive; includes current minute
    data = query_pg_minute_status(start_ts, end_ts)
    rates = get_rates()
    if data is None:
        return {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "minutes": minutes,
                "channels": {}, "error": True}
    # Trailing-60s request count per channel = current RPM (independent of the minute buckets).
    rpm_end = int(now.timestamp())
    rpm_map = query_pg_rpm(rpm_end - 60, rpm_end) or {}
    total_rpm = sum(rpm_map.values())
    bucket_dts = [first_min + timedelta(minutes=i) for i in range(minutes)]
    bucket_idx = [int(b.timestamp()) // 60 for b in bucket_dts]
    bucket_lbl = [b.strftime("%m-%d %H:%M") for b in bucket_dts]
    all_ids = sorted(set(rates.keys()) | set(data.keys()))
    channels = {}
    for ch_id in all_ids:
        chd = data.get(ch_id, {})
        cells = []
        for bi, lbl in zip(bucket_idx, bucket_lbl):
            c = chd.get(bi)
            if c:
                total = c["success"] + c["errors"]
                cells.append({"t": lbl, "success": c["success"], "errors": c["errors"],
                              "total": total, "rate": (c["success"] / total) if total else None})
            else:
                cells.append({"t": lbl, "success": 0, "errors": 0, "total": 0, "rate": None})
        channels[str(ch_id)] = {"name": rates.get(ch_id, {}).get("name", ""),
                                "rate": rates.get(ch_id, {}).get("rate", 0),
                                "rpm": rpm_map.get(ch_id, 0), "cells": cells}
    return {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "minutes": minutes,
            "channels": channels, "total_rpm": total_rpm, "error": False}

@app.post("/api/snapshot/hourly", dependencies=[Depends(require_auth)])
def api_snapshot_hourly():
    """Save snapshot for the previous completed hour. Called by internal scheduler."""
    now = now_shanghai()
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    end = start + timedelta(hours=1)
    raw = query_pg(int(start.timestamp()), int(end.timestamp()))
    if raw is None:
        return {"ok": False, "error": "数据库查询失败，跳过本次快照"}
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

@app.get("/api/system", dependencies=[Depends(require_auth)])
def api_system():
    # Inside the container psutil reads /proc, which on Linux reflects the host
    # machine's CPU/memory — i.e. the server's overall load.
    vm = psutil.virtual_memory()
    return {
        "cpu_percent": round(psutil.cpu_percent(interval=0.3), 1),
        "mem_percent": round(vm.percent, 1),
        "mem_used": vm.used,
        "mem_total": vm.total,
    }

# ── Auth & Settings ──
class LoginBody(BaseModel):
    password: str

class PasswordBody(BaseModel):
    old_password: str
    new_password: str

class WebhookBody(BaseModel):
    url: Optional[str] = None
    push_hourly: Optional[bool] = None
    push_daily: Optional[bool] = None
    push_error: Optional[bool] = None

@app.post("/api/login")
def api_login(body: LoginBody, request: Request):
    ip = _client_ip(request)
    now = time.time()
    # Prune stale entries so the in-memory dict can't grow unbounded across IPs.
    for k in list(_login_fails):
        kept = [t for t in _login_fails[k] if now - t < LOGIN_LOCKOUT_SECS]
        if kept:
            _login_fails[k] = kept
        else:
            del _login_fails[k]
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

@app.get("/api/settings/webhook", dependencies=[Depends(require_session)])
def api_get_webhook():
    return {
        "url": get_setting("webhook_url") or "",
        "push_hourly": (get_setting("webhook_push_hourly") or "0") == "1",
        "push_daily": (get_setting("webhook_push_daily") or "0") == "1",
        "push_error": (get_setting("webhook_push_error") or "0") == "1",
    }

@app.post("/api/settings/webhook", dependencies=[Depends(require_session)])
def api_set_webhook(body: WebhookBody):
    if body.url is not None:
        set_setting("webhook_url", body.url.strip())
    if body.push_hourly is not None:
        set_setting("webhook_push_hourly", "1" if body.push_hourly else "0")
    if body.push_daily is not None:
        set_setting("webhook_push_daily", "1" if body.push_daily else "0")
    if body.push_error is not None:
        set_setting("webhook_push_error", "1" if body.push_error else "0")
    return {"ok": True}

@app.post("/api/settings/webhook/test", dependencies=[Depends(require_session)])
def api_test_webhook():
    if not (get_setting("webhook_url") or "").strip():
        raise HTTPException(400, "请先配置 Webhook URL")
    ok = push_webhook("test", "✅ NewAPI Monitor Webhook 测试消息", {"hello": "world"})
    if not ok:
        raise HTTPException(502, "推送失败，请检查 URL 是否可达")
    return {"ok": True}

# ── Trend & rate history ──
@app.get("/api/trend", dependencies=[Depends(require_auth)])
def api_trend(days: int = 14, start: Optional[str] = None, end: Optional[str] = None):
    """Per-day totals. Either last `days` days (default) or an explicit [start, end] range."""
    today = now_shanghai().replace(hour=0, minute=0, second=0, microsecond=0)
    if start and end:
        try:
            d0 = datetime.strptime(start, "%Y-%m-%d")
            d1 = datetime.strptime(end, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
        if d1 < d0:
            d0, d1 = d1, d0
        date_list = []
        cur = d0
        while cur <= d1 and len(date_list) <= 366:
            date_list.append(cur)
            cur += timedelta(days=1)
    else:
        days = max(1, min(days, 90))
        date_list = [today - timedelta(days=i) for i in range(days, -1, -1)]

    todays = today.strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT date, total_real, total_usd, total_calls FROM daily_history"
        ).fetchall()
    hist = {r["date"]: dict(r) for r in rows}

    out = []
    for d in date_list:
        ds = d.strftime("%Y-%m-%d")
        if ds in hist:
            out.append(hist[ds])
        elif ds == todays:
            ch, tr, tu, tc, _m = load_snapshots(today, now_shanghai())
            out.append({"date": ds, "total_real": tr, "total_usd": tu, "total_calls": tc})
        else:
            out.append({"date": ds, "total_real": 0, "total_usd": 0, "total_calls": 0})
    return out

@app.get("/api/rate-history", dependencies=[Depends(require_auth)])
def api_rate_history(limit: int = 200):
    limit = max(1, min(limit, 1000))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT rh.id, rh.channel_id, c.name, rh.old_rate, rh.new_rate, rh.changed_at "
            "FROM rate_history rh LEFT JOIN channels c ON c.id = rh.channel_id "
            "ORDER BY rh.changed_at DESC, rh.id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

# ── 成本记录 (收入/支出，手动记账，独立页面，不与监控联动) ──
def _week_start_shanghai(date_str):
    """Return the Monday (Shanghai tz) for the given 'YYYY-MM-DD' string."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    monday = dt - timedelta(days=dt.weekday())  # weekday(): Mon=0..Sun=6
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)

@app.get("/api/cost/records", dependencies=[Depends(require_auth)])
def api_cost_records():
    """All cost records (income/expense), newest first."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, type, amount, date, note, created_at FROM cost_records "
            "ORDER BY date DESC, id DESC"
        ).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/cost/records", dependencies=[Depends(require_session)])
def api_cost_add(body: CostRecordBody):
    if body.type not in ("income", "expense"):
        raise HTTPException(400, "类型必须是 income 或 expense")
    if body.amount <= 0:
        raise HTTPException(400, "金额必须大于 0")
    try:
        datetime.strptime(body.date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO cost_records (type, amount, date, note) VALUES (?, ?, ?, ?)",
            (body.type, body.amount, body.date, body.note.strip()),
        )
        conn.commit()
    return {"ok": True}

@app.delete("/api/cost/records/{rid}", dependencies=[Depends(require_session)])
def api_cost_delete(rid: int):
    with get_db() as conn:
        cur = conn.execute("DELETE FROM cost_records WHERE id = ?", (rid,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "记录不存在")
    return {"ok": True}

@app.get("/api/cost/weekly", dependencies=[Depends(require_auth)])
def api_cost_weekly(weeks: int = 8):
    """Per-week aggregates. Each bucket covers Mon→Sun (Shanghai).
    Returns the last `weeks` weeks up to the current week, oldest first."""
    weeks = max(1, min(weeks, 52))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT type, amount, date FROM cost_records"
        ).fetchall()
    today_monday = _week_start_shanghai(now_shanghai().strftime("%Y-%m-%d"))
    buckets = []
    for i in range(weeks - 1, -1, -1):
        ws = today_monday - timedelta(weeks=i)
        we = ws + timedelta(days=7)
        buckets.append({
            "week_start": ws.strftime("%Y-%m-%d"),
            "week_end": (we - timedelta(days=1)).strftime("%Y-%m-%d"),
            "income": 0.0,
            "expense": 0.0,
        })
    for r in rows:
        try:
            ws = _week_start_shanghai(r["date"])
        except (ValueError, TypeError):
            continue
        delta_weeks = (today_monday - ws).days // 7
        if 0 <= delta_weeks < weeks:
            b = buckets[weeks - 1 - delta_weeks]
            if r["type"] == "income":
                b["income"] += r["amount"]
            elif r["type"] == "expense":
                b["expense"] += r["amount"]
    total_income = sum(b["income"] for b in buckets)
    total_expense = sum(b["expense"] for b in buckets)
    return {
        "weeks": buckets,
        "total_income": total_income,
        "total_expense": total_expense,
        "net": total_income - total_expense,
    }

# ── Report text builders & webhook push ──
def _channel_report_lines(channels):
    lines = ""
    for ch_id, d in sorted(channels.items(), key=lambda x: int(x[0])):
        usd = d.get("usd", 0) or 0
        rate = (d.get("real_cost", 0) / usd) if usd > 0 else 0
        name = d.get("name", "")
        head = f"渠道 {ch_id}" + (f" ({name})" if name else "")
        lines += f"\n\n📌 {head}（×{rate:.3f}）\n"
        lines += f"  调用    {d.get('calls', 0):,} 次\n"
        lines += f"  消费    ${usd:,.2f}\n"
        lines += f"  实付    ${d.get('real_cost', 0):,.2f}\n"
    return lines

def build_hourly_report(start_dt):
    """Build hourly report text/data from the completed-hour snapshot. Returns (None, None) if missing."""
    f = SNAPSHOT_DIR / f"{start_dt.strftime('%Y-%m-%d')}_{start_dt.hour:02d}.json"
    if not f.exists():
        return None, None
    snap = json.loads(f.read_text(encoding="utf-8"))
    end_dt = start_dt + timedelta(hours=1)
    rates = get_rates()
    channels = {cid: {**d, "name": rates.get(int(cid), {}).get("name", "")}
                for cid, d in snap.get("channels", {}).items()}
    text = (f"📊 NewAPI 消费小时报\n━━━━━━━━━━━━━━━━━\n"
            f"⏰ 时段: {start_dt.strftime('%m-%d %H:%M')} → {end_dt.strftime('%m-%d %H:%M')}\n"
            f"🔑 令牌: {TOKEN_NAME}\n━━━━━━━━━━━━━━━━━")
    text += _channel_report_lines(channels)
    text += "\n\n━━━━━━━━━━━━━━━━━\n"
    text += f"💎 本小时实付  ${snap.get('total_real', 0):,.2f}\n"
    text += f"📊 本小时消费  ${snap.get('total_usd', 0):,.2f}\n"
    text += f"📞 本小时调用  {snap.get('total_calls', 0):,} 次\n"
    text += "━━━━━━━━━━━━━━━━━"
    data = {"channels": snap.get("channels", {}),
            "total_real": snap.get("total_real", 0),
            "total_usd": snap.get("total_usd", 0),
            "total_calls": snap.get("total_calls", 0)}
    return text, data

def build_daily_report(date_str):
    """Build daily report text/data from snapshots, falling back to daily_history."""
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    channels, total_real, total_usd, total_calls, missing = load_snapshots(start, start + timedelta(days=1))
    if not channels:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM daily_history WHERE date = ?", (date_str,)).fetchone()
        if row:
            channels = json.loads(row["channels_json"]) if row["channels_json"] else {}
            total_real, total_usd, total_calls = row["total_real"], row["total_usd"], row["total_calls"]
            missing = []
    rates = get_rates()
    for cid, d in channels.items():
        d["name"] = rates.get(int(cid), {}).get("name", "")
    text = (f"📊 NewAPI 消费日报\n━━━━━━━━━━━━━━━━━\n"
            f"⏰ 日期: {date_str}\n🔑 令牌: {TOKEN_NAME}\n📐 方式: 小时报叠加\n━━━━━━━━━━━━━━━━━")
    text += _channel_report_lines(channels)
    text += "\n\n━━━━━━━━━━━━━━━━━\n"
    text += f"💎 实付合计  ${total_real:,.2f}\n"
    text += f"📊 消费合计  ${total_usd:,.2f}\n"
    text += f"📞 总调用    {total_calls:,} 次\n"
    if missing:
        text += f"⚠️ 缺失时段: {', '.join(missing)}\n"
    text += "━━━━━━━━━━━━━━━━━"
    data = {"channels": {str(k): v for k, v in channels.items()},
            "total_real": total_real, "total_usd": total_usd,
            "total_calls": total_calls, "missing": missing}
    return text, data

def push_webhook(report_type, text, data):
    """POST a generic JSON payload to the configured webhook. Returns True on 2xx."""
    url = (get_setting("webhook_url") or "").strip()
    if not url:
        return False
    payload = json.dumps({
        "type": report_type,
        "text": text,
        "data": data,
        "timestamp": now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[webhook] push failed: {e}", flush=True)
        return False

# ── 渠道错误率监控 ──
_error_alerting = {}  # channel_id -> True，当前处于告警态（用于判断「恢复」）；进程重启即清空

def _build_error_alert(ch_id, name, success, errors, rate, minute_label, recovered=False):
    head = f"渠道 {ch_id}" + (f" ({name})" if name else "")
    pct = f"{rate * 100:.1f}%"
    thr = f"{int(ERROR_RATE_THRESHOLD * 100)}%"
    if recovered:
        return (f"✅ NewAPI 渠道恢复\n━━━━━━━━━━━━━━━━━\n"
                f"📌 {head}\n⏰ 时段: {minute_label}\n"
                f"📉 错误率回落 {pct}（< {thr}）\n"
                f"  成功 {success:,} / 失败 {errors:,}\n"
                f"━━━━━━━━━━━━━━━━━")
    return (f"🚨 NewAPI 渠道错误率告警\n━━━━━━━━━━━━━━━━━\n"
            f"📌 {head}\n⏰ 时段: {minute_label}\n"
            f"⚠️ 错误率 {pct} ≥ {thr}\n"
            f"  失败 {errors:,} / 总计 {success + errors:,}\n"
            f"  成功 {success:,}\n"
            f"━━━━━━━━━━━━━━━━━")

def check_error_rates():
    """检查刚结束的一整分钟：每渠道错误率 ≥ 阈值则推送，回落到正常补发恢复通知。"""
    if (get_setting("webhook_push_error") or "0") != "1":
        return
    if not (get_setting("webhook_url") or "").strip():
        return
    now = now_shanghai()
    minute_start = now.replace(second=0, microsecond=0)
    window_start = minute_start - timedelta(minutes=1)
    rows = query_pg_error_rates(int(window_start.timestamp()), int(minute_start.timestamp()))
    if rows is None:
        return  # 查询失败，静默跳过本分钟（不把监控自身故障当成渠道故障）
    rates = get_rates()
    minute_label = f"{window_start.strftime('%m-%d %H:%M')} → {minute_start.strftime('%H:%M')}"
    for ch_id, d in rows.items():
        total = d["success"] + d["errors"]
        if total < ERROR_MIN_SAMPLE:
            continue  # 样本不足，不判定（告警态保持不变，等有足够样本的分钟再决定）
        rate = d["errors"] / total
        name = rates.get(ch_id, {}).get("name", "")
        payload = {"channel_id": ch_id, "name": name, "success": d["success"],
                   "errors": d["errors"], "total": total, "rate": rate}
        if rate >= ERROR_RATE_THRESHOLD:
            _error_alerting[ch_id] = True
            push_webhook("error_alert",
                         _build_error_alert(ch_id, name, d["success"], d["errors"], rate, minute_label),
                         payload)
        elif _error_alerting.pop(ch_id, None):
            # 之前在告警态，本分钟样本足够且已恢复正常 → 发恢复通知
            push_webhook("error_recovered",
                         _build_error_alert(ch_id, name, d["success"], d["errors"], rate, minute_label, recovered=True),
                         payload)


def _run_scheduler():
    """Every full hour: save the completed hour's snapshot, push hourly report,
    and at midnight finalize the previous day + push the daily report."""
    while True:
        now = now_shanghai()
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        time.sleep(max(1, (next_hour - now).total_seconds() + 30))
        try:
            res = api_snapshot_hourly()
            if not res.get("ok", True):
                print(f"[Scheduler] snapshot skipped: {res.get('error')}", flush=True)
        except Exception as e:
            print(f"[Scheduler] snapshot error: {e}", flush=True)
            continue

        now2 = now_shanghai()
        prev_start = now2.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        try:
            if (get_setting("webhook_push_hourly") or "0") == "1":
                text, d = build_hourly_report(prev_start)
                if text:
                    push_webhook("hourly", text, d)
        except Exception as e:
            print(f"[Scheduler] hourly push error: {e}", flush=True)

        if now2.hour == 0:  # crossed midnight — finalize yesterday
            try:
                yest = (now2 - timedelta(days=1)).strftime("%Y-%m-%d")
                start = datetime.strptime(yest, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
                ch, tr, tu, tc, _m = load_snapshots(start, start + timedelta(days=1))
                save_daily_history(yest, ch, tr, tu, tc)
                if (get_setting("webhook_push_daily") or "0") == "1":
                    text, d = build_daily_report(yest)
                    if text:
                        push_webhook("daily", text, d)
                cleanup_daily_snapshots(yest)
            except Exception as e:
                print(f"[Scheduler] daily error: {e}", flush=True)

def _run_error_monitor():
    """每分钟检查一次上一整分钟的渠道错误率。对齐分钟边界 +5s，给尾部日志写入留出落库时间。"""
    while True:
        now = now_shanghai()
        next_min = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        time.sleep(max(1, (next_min - now).total_seconds() + 5))
        try:
            check_error_rates()
        except Exception as e:
            print(f"[ErrorMonitor] error: {e}", flush=True)

BALANCE_REFRESH_SECS = 600  # 后台每 10 分钟刷新一次各渠道余额

def _run_balance_poller():
    """Refresh configured channels' balances shortly after startup, then every 10 minutes."""
    time.sleep(30)  # let startup settle before the first (network) refresh
    while True:
        try:
            results = refresh_all_balances()
            if results:
                print(f"[balance] refreshed {len(results)} channel(s)", flush=True)
        except Exception as e:
            print(f"[balance] poller error: {e}", flush=True)
        time.sleep(BALANCE_REFRESH_SECS)

@app.on_event("startup")
def _on_startup():
    try:
        _pg_pool.open()
    except Exception as e:
        print(f"[Startup] pg pool open error: {e}", flush=True)
    try:
        backfill_today_snapshots()
    except Exception as e:
        print(f"[Startup] backfill error: {e}", flush=True)
    try:
        cleanup_old_snapshots(7)
    except Exception as e:
        print(f"[Startup] cleanup error: {e}", flush=True)
    import threading
    threading.Thread(target=_run_scheduler, daemon=True).start()
    threading.Thread(target=_run_error_monitor, daemon=True).start()
    threading.Thread(target=_run_balance_poller, daemon=True).start()
    print("[Startup] scheduler started", flush=True)

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
    # The hourly scheduler + backfill are launched by the FastAPI startup event,
    # which uvicorn fires for both `python main.py` and `uvicorn main:app`.
    uvicorn.run(app, host="0.0.0.0", port=9217)