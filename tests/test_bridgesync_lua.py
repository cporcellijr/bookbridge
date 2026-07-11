"""Executable regression coverage for BridgeSync's pure Lua helpers."""

from pathlib import Path
import shutil
import subprocess


def test_bridgesync_lua_core_behaviors():
    repo = Path(__file__).resolve().parents[1]
    lua = shutil.which("lua") or shutil.which("lua5.4") or shutil.which("lua5.3")
    assert lua, "Lua is required to run BridgeSync plugin regression tests"

    result = subprocess.run(
        [
            lua,
            str(repo / "tests" / "lua" / "test_bridgesync_core.lua"),
            str(repo / "plugins" / "bridgesync.koplugin"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "BridgeSync Lua core tests passed" in result.stdout
