from datetime import timedelta


def channel_report_lines(channels):
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


def build_hourly_report_text(start_dt, token_name, channels, total_real, total_usd, total_calls):
    end_dt = start_dt + timedelta(hours=1)
    text = (
        "📊 NewAPI 消费小时报\n━━━━━━━━━━━━━━━━━\n"
        f"⏰ 时段: {start_dt.strftime('%m-%d %H:%M')} → {end_dt.strftime('%m-%d %H:%M')}\n"
        f"🔑 令牌: {token_name}\n━━━━━━━━━━━━━━━━━"
    )
    text += channel_report_lines(channels)
    text += "\n\n━━━━━━━━━━━━━━━━━\n"
    text += f"💎 本小时实付  ${total_real:,.2f}\n"
    text += f"📊 本小时消费  ${total_usd:,.2f}\n"
    text += f"📞 本小时调用  {total_calls:,} 次\n"
    text += "━━━━━━━━━━━━━━━━━"
    return text


def build_daily_report_text(date_str, token_name, channels, total_real, total_usd, total_calls, missing=None):
    text = (
        "📊 NewAPI 消费日报\n━━━━━━━━━━━━━━━━━\n"
        f"⏰ 日期: {date_str}\n🔑 令牌: {token_name}\n📐 方式: 小时报叠加\n━━━━━━━━━━━━━━━━━"
    )
    text += channel_report_lines(channels)
    text += "\n\n━━━━━━━━━━━━━━━━━\n"
    text += f"💎 实付合计  ${total_real:,.2f}\n"
    text += f"📊 消费合计  ${total_usd:,.2f}\n"
    text += f"📞 总调用    {total_calls:,} 次\n"
    if missing:
        text += f"⚠️ 缺失时段: {', '.join(missing)}\n"
    text += "━━━━━━━━━━━━━━━━━"
    return text
