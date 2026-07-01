from datetime import datetime, timedelta, timezone
import unittest

from app.report_text import build_daily_report_text, build_hourly_report_text


SHANGHAI = timezone(timedelta(hours=8))


class ReportTextTests(unittest.TestCase):
    def test_build_hourly_report_text_uses_token_and_channel_names(self):
        start = datetime(2026, 7, 1, 8, 0, tzinfo=SHANGHAI)
        text = build_hourly_report_text(
            start,
            token_name="token-a",
            channels={
                "2": {"name": "主渠道", "calls": 3, "usd": 10, "real_cost": 0.4},
            },
            total_real=0.4,
            total_usd=10,
            total_calls=3,
        )

        self.assertIn("NewAPI 消费小时报", text)
        self.assertIn("token-a", text)
        self.assertIn("渠道 2 (主渠道)", text)
        self.assertIn("$0.40", text)

    def test_build_daily_report_text_includes_missing_hours(self):
        text = build_daily_report_text(
            "2026-07-01",
            token_name="token-a",
            channels={},
            total_real=0,
            total_usd=0,
            total_calls=0,
            missing=["03:00", "04:00"],
        )

        self.assertIn("NewAPI 消费日报", text)
        self.assertIn("缺失时段: 03:00, 04:00", text)


if __name__ == "__main__":
    unittest.main()
