"""Each module imports in a fresh interpreter, with nothing loaded before it."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(code):
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)


@pytest.mark.parametrize("module", [
    "src.services.llm_matching",
    "src.services.suggestions_service",
    "src.api.kosync_server",
])
def test_module_imports_in_a_fresh_interpreter(module):
    result = _run(f"import {module}")
    assert result.returncode == 0, (result.stderr.strip().splitlines() or [""])[-1]


def test_web_server_registers_the_position_preview_route():
    result = _run(
        "import src.web_server as w\n"
        "from flask import Flask\n"
        "app = Flask('t')\n"
        "app.register_blueprint(w.kosync_admin_bp)\n"
        "print(any(r.rule == '/api/books/<abs_id>/position-preview' for r in app.url_map.iter_rules()))\n"
    )
    assert result.returncode == 0, (result.stderr.strip().splitlines() or [""])[-1]
    assert result.stdout.strip().splitlines()[-1] == "True"
