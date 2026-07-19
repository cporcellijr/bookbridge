from pathlib import Path


def test_storyteller_results_do_not_interpolate_remote_fields_into_html():
    source = Path("static/js/storyteller-modal.js").read_text(encoding="utf-8")

    assert "${book.title}" not in source
    assert "${book.authors" not in source
    assert "onclick=\"linkStoryteller('${book.uuid}')\"" not in source
    assert "title.textContent = book.title" in source
    assert "button.addEventListener('click'" in source


def test_request_body_limit_is_configured():
    source = Path("src/web_server.py").read_text(encoding="utf-8")

    assert 'app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024' in source
