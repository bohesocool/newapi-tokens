from pathlib import Path
import unittest


class StatusTooltipTemplateTests(unittest.TestCase):
    def test_status_bar_tooltips_are_plain_text(self):
        # Tooltip rendering lives in the static JS after the template split.
        js = Path("app/static/js/dashboard.js").read_text(encoding="utf-8")

        self.assertIn("_csTip.textContent = bar.dataset.tip", js)
        self.assertNotIn("tip = `<div", js)
        self.assertIn("错误率", js)
        self.assertIn("失败 ${errors.toLocaleString()} 次", js)


if __name__ == "__main__":
    unittest.main()
