"""Who may see the server's local ebook folder through the Suggestions page."""

from typing import Any

from src.utils.config_loader import env_truthy
from src.utils.user_context import get_current_user_id


def acting_user_is_regular(database_service: Any) -> bool:
    """True when a signed-in non-admin user is the acting user (no user -> False)."""
    user_id = get_current_user_id()
    if user_id is None or database_service is None:
        return False
    user = database_service.get_user(user_id)
    return bool(user) and not user.is_admin


def suggestions_exclude_local_files(database_service: Any) -> bool:
    """Scans omit Local File when the admin setting is on or the acting user is regular."""
    return env_truthy("SUGGESTIONS_EXCLUDE_LOCAL_FILES") or acting_user_is_regular(database_service)
