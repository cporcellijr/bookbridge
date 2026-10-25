"""SMIL_SKIP_CTC is registered everywhere a setting must be, and is off unless set."""

from pathlib import Path

from src.services.alignment_service import AlignmentService

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_key_is_in_all_four_touchpoints():
    config_loader_src = (REPO_ROOT / "src" / "utils" / "config_loader.py").read_text(encoding="utf-8")
    web_server_src = (REPO_ROOT / "src" / "web_server.py").read_text(encoding="utf-8")
    settings_html = (REPO_ROOT / "templates" / "settings.html").read_text(encoding="utf-8")

    # ALL_SETTINGS and DEFAULT_CONFIG.
    assert config_loader_src.count("SMIL_SKIP_CTC") >= 2
    assert "'SMIL_SKIP_CTC': 'false'" in config_loader_src
    # bool_keys in the POST /settings handler.
    assert "'SMIL_SKIP_CTC'" in web_server_src
    # The settings page.
    assert 'name="SMIL_SKIP_CTC"' in settings_html
    assert "get_bool('SMIL_SKIP_CTC')" in settings_html


def test_true_and_on_both_enable_it(monkeypatch):
    for spelling in ("true", "TRUE", "on", "On", "1", "yes"):
        monkeypatch.setenv("SMIL_SKIP_CTC", spelling)
        assert AlignmentService.smil_skip_ctc_enabled() is True, spelling


def test_false_and_unset_both_disable_it(monkeypatch):
    monkeypatch.setenv("SMIL_SKIP_CTC", "false")
    assert AlignmentService.smil_skip_ctc_enabled() is False

    monkeypatch.delenv("SMIL_SKIP_CTC", raising=False)
    assert AlignmentService.smil_skip_ctc_enabled() is False
