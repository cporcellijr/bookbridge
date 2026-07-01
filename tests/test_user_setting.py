import os
import unittest

from src.utils.user_config import user_setting
from src.utils import user_context


class TestUserSetting(unittest.TestCase):
    def tearDown(self):
        user_context.set_current_user_credentials(None)
        os.environ.pop("ABS_LIBRARY_ID", None)

    def test_falls_back_to_env_without_context(self):
        os.environ["ABS_LIBRARY_ID"] = "global-lib"
        user_context.set_current_user_credentials(None)
        self.assertEqual(user_setting("ABS_LIBRARY_ID"), "global-lib")

    def test_per_user_value_wins(self):
        os.environ["ABS_LIBRARY_ID"] = "global-lib"
        user_context.set_current_user_credentials({"ABS_LIBRARY_ID": "user-lib"})
        self.assertEqual(user_setting("ABS_LIBRARY_ID"), "user-lib")

    def test_blank_user_value_falls_back(self):
        os.environ["ABS_LIBRARY_ID"] = "global-lib"
        user_context.set_current_user_credentials({"ABS_LIBRARY_ID": ""})
        self.assertEqual(user_setting("ABS_LIBRARY_ID"), "global-lib")

    def test_missing_key_in_creds_falls_back(self):
        os.environ["ABS_LIBRARY_ID"] = "global-lib"
        user_context.set_current_user_credentials({"ABS_KEY": "tok"})
        self.assertEqual(user_setting("ABS_LIBRARY_ID"), "global-lib")

    def test_default_when_unset(self):
        os.environ.pop("ABS_LIBRARY_ID", None)
        user_context.set_current_user_credentials(None)
        self.assertEqual(user_setting("ABS_LIBRARY_ID", "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
