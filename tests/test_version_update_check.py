"""Tests for the in-app update checker (src/version.py).

Issue #290 Bug 3: the update check hardcoded `cporcellijr/bookbridge`, but that
rename is still pending, so every check 404'd. It must hit the real repo
(APP_REPO, defaulting to the current name) so the version comparison works.
"""

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _fresh_version_module(env):
    """Import a clean copy of src.version under a patched environment."""
    with patch.dict(os.environ, env, clear=False):
        sys.modules.pop("src.version", None)
        return importlib.import_module("src.version")


def test_update_check_queries_configured_repo():
    mod = _fresh_version_module({"APP_REPO": "cporcellijr/abs-kosync-bridge",
                                 "APP_VERSION": "6.7.0"})
    mod._update_cache, mod._last_check = None, 0

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"tag_name": "v6.8"}
    with patch.object(mod.requests, "get", return_value=resp) as mock_get:
        latest, available = mod.get_update_status()

    url = mock_get.call_args.args[0]
    assert "cporcellijr/abs-kosync-bridge" in url
    assert "bookbridge" not in url
    assert latest == "6.8"
    assert available is True  # 6.7.0 installed, 6.8 released


def test_update_check_default_repo_is_not_the_pending_rename():
    mod = _fresh_version_module({"APP_VERSION": "6.8"})
    assert mod.APP_REPO == "cporcellijr/abs-kosync-bridge"


def test_dev_build_never_reports_update_available():
    mod = _fresh_version_module({"APP_VERSION": "dev"})
    mod._update_cache, mod._last_check = None, 0

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"tag_name": "v6.8"}
    with patch.object(mod.requests, "get", return_value=resp):
        _latest, available = mod.get_update_status()
    assert available is False  # dev builds opt out of the update banner


def teardown_module(_module):
    # Restore a clean import for any later tests that touch src.version.
    sys.modules.pop("src.version", None)
