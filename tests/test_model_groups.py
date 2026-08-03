import unittest

from app.model_groups import model_group


class ModelGroupTests(unittest.TestCase):
    def test_mini_and_luna_models_share_the_mini_group(self):
        self.assertEqual(model_group("gpt-5-mini"), "mini")
        self.assertEqual(model_group("gpt-5-luna"), "mini")

    def test_luna_must_be_a_suffix(self):
        self.assertEqual(model_group("gpt-5-luna-preview"), "other")

    def test_other_and_null_models_are_not_mini(self):
        self.assertEqual(model_group("gpt-5-pro"), "other")
        self.assertIsNone(model_group(None))


if __name__ == "__main__":
    unittest.main()
