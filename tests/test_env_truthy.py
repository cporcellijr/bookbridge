import os
import unittest
from unittest.mock import patch

from src.utils.config_loader import env_truthy


class TestEnvTruthy(unittest.TestCase):
    def test_accepts_all_truthy_spellings(self):
        for val in ("true", "True", "TRUE", "1", "yes", "YES", "on", "On", " on "):
            with patch.dict(os.environ, {"FLAG": val}):
                self.assertTrue(env_truthy("FLAG"), f"{val!r} should be truthy")

    def test_rejects_falsey_values(self):
        for val in ("false", "0", "no", "off", "", "  ", "nope"):
            with patch.dict(os.environ, {"FLAG": val}):
                self.assertFalse(env_truthy("FLAG"), f"{val!r} should be falsey")

    def test_unset_uses_default(self):
        os.environ.pop("MISSING_FLAG", None)
        self.assertFalse(env_truthy("MISSING_FLAG"))
        self.assertTrue(env_truthy("MISSING_FLAG", "true"))
        self.assertTrue(env_truthy("MISSING_FLAG", "on"))


if __name__ == "__main__":
    unittest.main()
