import os
import re
import subprocess
import requests
import logging
import time

# GitHub repo used for commit-count and update checks. Overridable via APP_REPO.
APP_REPO = os.environ.get("APP_REPO", "cporcellijr/bookbridge")

def _get_commit_count():
    """Resolve commit count from env, local git metadata, or GitHub branch history."""
    for key in ("APP_COMMIT_COUNT", "GIT_COMMIT_COUNT", "COMMIT_COUNT", "BUILD_NUMBER"):
        value = os.environ.get(key, "").strip()
        if value.isdigit():
            return value

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False
        )
        count = (result.stdout or "").strip()
        if result.returncode == 0 and count.isdigit():
            return count
    except Exception:
        pass

    # Fallback for runtime environments without a mounted .git directory.
    repo = APP_REPO
    branch = os.environ.get("APP_BRANCH", "dev")
    try:
        response = requests.get(
            f"https://api.github.com/repos/{repo}/commits",
            params={"sha": branch, "per_page": 1},
            timeout=5,
            headers={"Accept": "application/vnd.github+json"}
        )
        if response.status_code == 200:
            link = response.headers.get("Link", "")
            match = re.search(r"[?&]page=(\d+)>; rel=\"last\"", link)
            if match:
                return match.group(1)
            data = response.json()
            if isinstance(data, list) and data:
                return "1"
    except Exception:
        pass

    return None


_raw_version = os.environ.get("APP_VERSION", "dev")
if _raw_version == "dev":
    _commit_count = _get_commit_count()
    APP_VERSION = f"dev {_commit_count}" if _commit_count else "dev"
else:
    APP_VERSION = _raw_version

_update_cache = None
_last_check = 0
_CHECK_INTERVAL = 86400  # 24 hours

logger = logging.getLogger(__name__)


def get_update_status():
    """Returns (latest_version, update_available) — refreshes every 24 hours."""
    global _update_cache, _last_check
    now = time.time()

    if _update_cache is not None and (now - _last_check) < _CHECK_INTERVAL:
        return _update_cache

    try:
        r = requests.get(
            f"https://api.github.com/repos/{APP_REPO}/releases/latest",
            timeout=5,
            headers={"Accept": "application/vnd.github+json"}
        )
        if r.status_code == 200:
            latest = r.json().get("tag_name", "").lstrip("v")
            is_dev = APP_VERSION.startswith("dev")
            available = (latest != APP_VERSION) and not is_dev

            _update_cache = (latest, available)
            _last_check = now
            logger.debug(f"Update check: current={APP_VERSION}, latest={latest}, update_available={available}")
            return _update_cache

    except Exception as e:
        logger.debug(f"Update check failed: {e}")
        if _update_cache is not None:
            return _update_cache

    _update_cache = (None, False)
    _last_check = now
    return _update_cache
