"""The same edition-dialog behavior ships in both UI shells."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for frontend regression tests")
def test_provider_edition_picker_javascript():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([shutil.which("node"), "--test", "tests/test_provider_edition_picker.js"],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("template", ["suggestions.html", "next/suggestions.html"])
def test_both_ui_shells_include_the_shared_dialog(template):
    root = Path(__file__).resolve().parents[1]
    text = (root / "templates" / template).read_text()
    assert "{% include '_provider_edition_picker.html' %}" in text
    assert "/static/css/provider-edition-picker.css" in text
