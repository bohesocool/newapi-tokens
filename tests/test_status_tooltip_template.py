from pathlib import Path
import unittest


class StatusTooltipTemplateTests(unittest.TestCase):
    def test_status_bar_tooltips_are_plain_text(self):
        html = Path("app/templates/index.html").read_text(encoding="utf-8")

        self.assertIn("_csTip.textContent = bar.dataset.tip", html)
        self.assertNotIn("tip = `<div", html)
        self.assertIn("错误率", html)
        self.assertIn("失败 ${errors.toLocaleString()} 次", html)


if __name__ == "__main__":
    unittest.main()
