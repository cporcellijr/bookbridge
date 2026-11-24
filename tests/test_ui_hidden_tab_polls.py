"""Polling pages (Logs in both interfaces, Classic Library's Forge banner) stop while the tab is hidden."""

from pathlib import Path
import shutil
import subprocess
import unittest

_ROOT = Path(__file__).resolve().parent.parent


class TestHiddenTabPolls(unittest.TestCase):
    def _run(self, script: str) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required to execute the page scripts")
        result = subprocess.run(
            [node, str(_ROOT / "tests" / script)],
            capture_output=True, text=True, timeout=15, cwd=_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_logs_live_and_auto_refresh_skip_hidden_tabs_and_catch_up_when_shown(self):
        self._run("test_ui_logs_live.js")

    def test_classic_forge_banner_skips_hidden_tabs_and_checks_when_shown(self):
        self._run("test_classic_forge_poll.js")


if __name__ == "__main__":
    unittest.main()
