"""Search from the Maintenance tab shows editable settings, so it must show Save too."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_search_from_maintenance_restores_the_save_bar():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to execute the settings handlers')
    script = Path(__file__).with_suffix('.js')
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
