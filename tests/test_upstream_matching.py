import unittest

try:
    from app.main import _match_upstream_key, _select_local_credential_group
except ModuleNotFoundError:
    # The production image copies app/ to /app rather than retaining the package
    # directory, so container-side smoke tests import main directly.
    from main import _match_upstream_key, _select_local_credential_group


class UpstreamKeyMatchingTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            {
                "name": "plus稳定",
                "rate": 0.06,
                "status": "active",
                "group": {"name": "ChatGPT-Plus【高并发-稳定通道】"},
            },
            {
                "name": "兜底渠道",
                "rate": 0.15,
                "status": "active",
                "group": {"name": "ChatGPT-Pro【高并发-兜底通道】"},
            },
            {
                "name": "特惠",
                "rate": 0.05,
                "status": "active",
                "group": {"name": "ChatGPT-Plus【高并发-特惠通道】"},
            },
        ]

    def test_empty_channel_name_does_not_match_first_key(self):
        self.assertIsNone(_match_upstream_key({"name": ""}, self.items))
        self.assertIsNone(_match_upstream_key({"name": ""}, self.items[:1]))

    def test_specialized_channel_names_select_their_matching_key(self):
        discount = _match_upstream_key({"name": "麻豆plus特惠"}, self.items)
        fallback = _match_upstream_key({"name": "麻豆pro兜底"}, self.items)

        self.assertEqual((discount["name"], discount["rate"]), ("特惠", 0.05))
        self.assertEqual((fallback["name"], fallback["rate"]), ("兜底渠道", 0.15))

    def test_remote_url_group_reuses_credentials_from_overlapping_old_url_group(self):
        local_groups = {
            "https://mdkj.lol": [
                {
                    "id": 25,
                    "bal_type": "sub2api",
                    "bal_url": "https://mdkj.lol",
                    "bal_account": "account@example.com",
                    "bal_password": "password",
                    "bal_rt": "",
                },
                {
                    "id": 29,
                    "bal_type": "sub2api",
                    "bal_url": "https://mdkj.lol",
                    "bal_account": "",
                    "bal_password": "",
                    "bal_rt": "",
                },
            ],
        }

        selected = _select_local_credential_group(
            "https://vip.mdkj.lol", {"29"}, local_groups
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["representative"]["id"], 25)
        self.assertEqual(selected["query_url"], "https://vip.mdkj.lol")


if __name__ == "__main__":
    unittest.main()
