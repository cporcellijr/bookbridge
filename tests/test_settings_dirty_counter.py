"""Exercise the actual settings handlers when an edit is undone (#506)."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_settings_counter_and_warning_clear_when_edits_are_restored():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the settings handlers')
    script = Path(__file__).with_suffix('.js')
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
