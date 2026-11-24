"""Logs live mode (shared by Classic and New) stops polling while the tab is hidden."""

from pathlib import Path
import shutil
import subprocess
import unittest

_ROOT = Path(__file__).resolve().parent.parent


class TestLogsLiveMode(unittest.TestCase):
    def test_live_mode_skips_hidden_tabs_and_catches_up_when_shown(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required to execute the Logs script")
        result = subprocess.run(
            [node, str(Path(__file__).with_suffix(".js"))],
            capture_output=True, text=True, timeout=15, cwd=_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
