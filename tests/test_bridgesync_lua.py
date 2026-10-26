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


def test_bridgesync_real_init_can_log_sqlite_startup(tmp_path):
    """The real plugin init path must establish its log path before logging."""
    repo = Path(__file__).resolve().parents[1]
    lua = shutil.which("lua") or shutil.which("lua5.4") or shutil.which("lua5.3")
    assert lua, "Lua is required to run BridgeSync plugin regression tests"

    result = subprocess.run(
        [
            lua,
            str(repo / "tests" / "lua" / "test_bridgesync_init.lua"),
            str(repo / "plugins" / "bridgesync.koplugin"),
            str(tmp_path),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "BridgeSync Lua init regression test passed" in result.stdout


def test_bridgesync_testauth_server_identity_probe():
    """BridgeSync testAuth must correctly identify BookBridge vs KoSync-compatible servers."""
    repo = Path(__file__).resolve().parents[1]
    lua = shutil.which("lua") or shutil.which("lua5.4") or shutil.which("lua5.3")
    assert lua, "Lua is required to run BridgeSync plugin regression tests"

    result = subprocess.run(
        [
            lua,
            str(repo / "tests" / "lua" / "test_bridgesync_testauth.lua"),
            str(repo / "plugins" / "bridgesync.koplugin"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "BridgeSync Lua testAuth regression tests passed" in result.stdout


def test_bridgesync_book_status_sidecar_rules():
    """Sidecar status read/write: creates a sidecar for a never-opened book,
    leaves the open document alone, and never touches position data."""
    repo = Path(__file__).resolve().parents[1]
    lua = shutil.which("lua") or shutil.which("lua5.4") or shutil.which("lua5.3")
    assert lua, "Lua is required to run BridgeSync plugin regression tests"

    result = subprocess.run(
        [
            lua,
            str(repo / "tests" / "lua" / "test_bridge_book_status.lua"),
            str(repo / "plugins" / "bridgesync.koplugin"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "BridgeSync book-status Lua tests passed" in result.stdout


def test_bridgesync_read_history_merge():
    """Other devices' reads are filed into KOReader's History, bounded so a
    first sync cannot bury the device's real history."""
    repo = Path(__file__).resolve().parents[1]
    lua = shutil.which("lua") or shutil.which("lua5.4") or shutil.which("lua5.3")
    assert lua, "Lua is required to run BridgeSync plugin regression tests"

    result = subprocess.run(
        [
            lua,
            str(repo / "tests" / "lua" / "test_bridge_read_history.lua"),
            str(repo / "plugins" / "bridgesync.koplugin"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "BridgeSync reading-history merge tests passed" in result.stdout


def test_bridgesync_hash_pairs():
    """History books report their sidecar content hash and basename hash, never touching files."""
    repo = Path(__file__).resolve().parents[1]
    lua = shutil.which("lua") or shutil.which("lua5.4") or shutil.which("lua5.3")
    assert lua, "Lua is required to run BridgeSync plugin regression tests"

    result = subprocess.run(
        [
            lua,
            str(repo / "tests" / "lua" / "test_bridge_hash_pairs.lua"),
            str(repo / "plugins" / "bridgesync.koplugin"),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "BridgeSync hash-pairs Lua tests passed" in result.stdout
