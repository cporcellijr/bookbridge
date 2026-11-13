"""Lazy web-server auth delegators for extracted Flask routes."""

from functools import wraps
from typing import Any, Callable

_WEB_MODULE = None


def bind_web_module(web_module: Any) -> None:
    """Bind the active entry module, including production's ``__main__``."""
    global _WEB_MODULE
    _WEB_MODULE = web_module


def _web_server() -> Any:
    """Return the active entry module without importing it at module load time."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module
    return import_module('src.web_server')


def admin_required(view: Callable) -> Callable:
    """Delegate authorization to the existing guard at request time."""
    @wraps(view)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        return _web_server().admin_required(view)(*args, **kwargs)
    return guarded
