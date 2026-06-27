#!/usr/bin/env python3
"""Cron hook: save hourly snapshot then generate report text.
Called by the existing cron job wrapper."""
import sys, os, json, subprocess, urllib.request
from datetime import datetime, timezone, timedelta

SHANGHAI = timezone(timedelta(hours=8))
MONITOR_URL = os.environ.get("MONITOR_URL", "http://localhost:9217")
API_KEY = os.environ.get("MONITOR_API_KEY", "")

def api_open(path, method="GET", timeout=10):
    """Open a monitor API endpoint with the API key attached."""
    req = urllib.request.Request(f"{MONITOR_URL}{path}", method=method)
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    return urllib.request.urlopen(req, timeout=timeout)

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
        resp = api_open("/api/hourly")
        data = json.loads(resp.read().decode())
        # Generate report text
        title = "📊 NewAPI 消费小时报"
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        end = start + timedelta(hours=1)
        report = f"{title}\n━━━━━━━━━━━━━━━━━\n⏰ 时段: {start.strftime('%m-%d %H:%M')} → {end.strftime('%m-%d %H:%M')}\n🔑 令牌: ducker\n━━━━━━━━━━━━━━━━━"

        # Read the completed hour's snapshot via API (decoupled from filesystem layout)
        try:
            snap_resp = api_open(f"/api/snapshot/{start.strftime('%Y-%m-%d')}/{start.hour:02d}")
            snap = json.loads(snap_resp.read().decode())
            channels = snap.get("channels", {})
            cur_real = snap.get("total_real", 0)
            cur_usd = snap.get("total_usd", 0)
            cur_calls = snap.get("total_calls", 0)
            if channels:
                for ch_id, d in sorted(channels.items(), key=lambda x: int(x[0])):
                    rate = d.get("real_cost",0) / d.get("usd",1) if d.get("usd",0) > 0 else 0
                    report += f"\n\n📌 渠道 {ch_id}（×{rate:.3f}）\n"
                    report += f"  调用    {d['calls']:,} 次\n"
                    report += f"  消费    ${d['usd']:,.2f}\n"
                    report += f"  实付    ${d['real_cost']:,.2f}\n"
        except Exception:
            # Fallback to current_hour if snapshot not ready yet
            cur = data.get("current_hour", {})
            channels = cur.get("channels", {})
            cur_real = cur.get("total_real", 0)
            cur_usd = cur.get("total_usd", 0)
            cur_calls = cur.get("total_calls", 0)
            if channels:
                for ch_id, d in sorted(channels.items(), key=lambda x: int(x[0])):
                    report += f"\n\n📌 渠道 {ch_id}（×{d.get('rate',0)}）\n"
                    report += f"  调用    {d['calls']:,} 次\n"
                    report += f"  消费    ${d['usd']:,.2f}\n"
                    report += f"  实付    ${d['real_cost']:,.2f}\n"

        report += f"\n\n━━━━━━━━━━━━━━━━━\n"
        report += f"💎 本小时实付  ${cur_real:,.2f}\n"
        report += f"📊 本小时消费  ${cur_usd:,.2f}\n"
        report += f"📞 本小时调用  {cur_calls:,} 次\n"

        today = data.get("today_total", {})
        report += f"\n💎 今日累计实付  ${today.get('total_real',0):,.2f}\n"
        report += f"📊 今日累计消费  ${today.get('total_usd',0):,.2f}\n"
        report += f"📞 今日累计调用  {today.get('total_calls',0):,} 次\n"
        report += f"━━━━━━━━━━━━━━━━━"

        print(report)

    elif report_type == "12h":
        now_str = now.strftime("%Y-%m-%d")
        resp = api_open("/api/hourly")
        data = json.loads(resp.read().decode())
        title = "📊 NewAPI 消费 12 小时报"
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=12)
        end = start + timedelta(hours=12)
        report = f"{title}\n━━━━━━━━━━━━━━━━━\n⏰ 时段: {start.strftime('%m-%d %H:%M')} → {end.strftime('%m-%d %H:%M')}\n🔑 令牌: ducker\n📐 方式: 小时报叠加\n━━━━━━━━━━━━━━━━━"

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
        resp = api_open(f"/api/daily/{yesterday}")
        data = json.loads(resp.read().decode())

        title = "📊 NewAPI 消费日报"
        report = f"{title}\n━━━━━━━━━━━━━━━━━\n⏰ 日期: {yesterday}\n🔑 令牌: ducker\n📐 方式: 小时报叠加\n━━━━━━━━━━━━━━━━━"

        channels = data.get("channels", {})
        if channels:
            for ch_id, d in sorted(channels.items(), key=lambda x: int(x[0])):
                report += f"\n\n📌 渠道 {ch_id} ({d.get('name','')})\n"
                report += f"  调用    {d.get('calls',0):,} 次\n"
                report += f"  消费    ${d.get('usd',0):,.2f}\n"
                report += f"  实付    ${d.get('real_cost',0):,.2f}\n"

        report += f"\n\n━━━━━━━━━━━━━━━━━\n"
        report += f"💎 实付合计  ${data.get('total_real',0):,.2f}\n"
        report += f"📊 消费合计  ${data.get('total_usd',0):,.2f}\n"
        report += f"📞 总调用    {data.get('total_calls',0):,} 次\n"
        if data.get("missing"):
            report += f"⚠️ 缺失时段: {', '.join(data['missing'])}\n"
        report += f"━━━━━━━━━━━━━━━━━"

        print(report)

except Exception as e:
    print(f"❌ Report generation failed: {e}")
    sys.exit(1)