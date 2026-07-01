#!/usr/bin/env python3
"""Cron hook: ask the monitor API for report text.

The FastAPI app owns report formatting; this script stays as a thin cron wrapper.
"""
import sys, os, json, urllib.error, urllib.request
from datetime import datetime, timezone, timedelta

SHANGHAI = timezone(timedelta(hours=8))
MONITOR_URL = os.environ.get("MONITOR_URL", "http://localhost:9217")
API_KEY = os.environ.get("MONITOR_API_KEY", "")
TOKEN_NAME = os.environ.get("TOKEN_NAME", "ducker")

def api_open(path, method="GET", timeout=10):
    """Open a monitor API endpoint with the API key attached."""
    req = urllib.request.Request(f"{MONITOR_URL}{path}", method=method)
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    return urllib.request.urlopen(req, timeout=timeout)

def api_json(path, method="GET", timeout=10):
    resp = api_open(path, method=method, timeout=timeout)
    return json.loads(resp.read().decode())

def print_report(path):
    data = api_json(path)
    print(data["text"])

report_type = sys.argv[1] if len(sys.argv) > 1 else "hourly"
now = datetime.now(SHANGHAI)

# 1. Touch the monitor API to trigger snapshot save
try:
    urllib.request.urlopen(f"{MONITOR_URL}/api/health", timeout=5)
except:
    pass

# 2. If daily, finalize first (save history + cleanup old snapshots)
if report_type == "daily":
    try:
        resp = api_open("/api/finalize-daily", method="POST")
        result = json.loads(resp.read().decode())
    except Exception as e:
        print(f"❌ Finalize daily failed: {e}")
        sys.exit(1)

# 3. Query the monitor API for the report data
try:
    if report_type == "hourly":
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        path = f"/api/report/hourly/{start.strftime('%Y-%m-%d')}/{start.hour:02d}"
        try:
            print_report(path)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            api_json("/api/snapshot/hourly", method="POST", timeout=30)
            print_report(path)

    elif report_type == "12h":
        now_str = now.strftime("%Y-%m-%d")
        resp = api_open("/api/hourly")
        data = json.loads(resp.read().decode())
        title = "📊 NewAPI 消费 12 小时报"
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=12)
        end = start + timedelta(hours=12)
        report = f"{title}\n━━━━━━━━━━━━━━━━━\n⏰ 时段: {start.strftime('%m-%d %H:%M')} → {end.strftime('%m-%d %H:%M')}\n🔑 令牌: {TOKEN_NAME}\n📐 方式: 小时报叠加\n━━━━━━━━━━━━━━━━━"

        channels = data.get("channels", {})
        total_real = data.get("today_total",{}).get("total_real",0)
        total_usd = data.get("today_total",{}).get("total_usd",0)
        total_calls = data.get("today_total",{}).get("total_calls",0)

        if channels:
            for ch_id, d in sorted(channels.items(), key=lambda x: int(x[0])):
                report += f"\n\n📌 渠道 {ch_id} ({d.get('name','')})\n"
                report += f"  调用    {d.get('calls',0):,} 次\n"
                report += f"  消费    ${d.get('usd',0):,.2f}\n"
                report += f"  实付    ${d.get('real_cost',0):,.2f}\n"

        report += f"\n\n━━━━━━━━━━━━━━━━━\n"
        report += f"💎 实付合计  ${total_real:,.2f}\n"
        report += f"📊 消费合计  ${total_usd:,.2f}\n"
        report += f"📞 总调用    {total_calls:,} 次\n"
        report += f"━━━━━━━━━━━━━━━━━"

        print(report)

    elif report_type == "daily":
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        print_report(f"/api/report/daily/{yesterday}")

except Exception as e:
    print(f"❌ Report generation failed: {e}")
    sys.exit(1)
