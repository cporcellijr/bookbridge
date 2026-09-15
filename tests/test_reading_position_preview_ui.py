from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_preview_assets_are_scoped_to_library_page():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    index = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "{% if active_page == 'library' %}" in base
    assert '/static/css/reading-position-preview.css' in base
    assert '/static/js/reading-position-preview.js' in base
    assert "{% set active_page = 'library' %}" in index


def test_preview_script_builds_local_accessible_ui_without_dom_relocation():
    script = (ROOT / "static" / "js" / "reading-position-preview.js").read_text(encoding="utf-8")

    assert "card.querySelector('.book-info')" in script
    assert "card.dataset.syncMode === 'audiobook_only'" in script
    assert "panel.setAttribute('role', 'status')" in script
    assert "panel.setAttribute('aria-live', 'polite')" in script
    assert "data-position-preview-toggle" in script
    assert "position-preview-panel" in script
    assert "append(button, panel)" in script
    assert "appendChild" not in script


def test_preview_script_renders_book_text_as_text_only():
    script = (ROOT / "static" / "js" / "reading-position-preview.js").read_text(encoding="utf-8")

    assert '.textContent' in script
    assert '.innerHTML' not in script
    assert "cache: 'no-store'" in script
    assert "encodeURIComponent(absId)" in script
    assert "console.log" not in script
    assert "console.error" not in script


def test_expanded_preview_only_disables_grid_stretch_while_needed():
    script = (ROOT / "static" / "js" / "reading-position-preview.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "reading-position-preview.css").read_text(encoding="utf-8")

    assert "button.closest('.book-grid')" in script
    assert "[data-position-preview-toggle][aria-expanded=\"true\"]" in script
    assert "grid.classList.toggle('position-preview-expanded', hasExpandedPreview)" in script
    assert ".book-grid.position-preview-expanded" in css
    assert "align-items: start" in css


def test_preview_css_uses_existing_bookbridge_tokens_and_has_mobile_layout():
    css = (ROOT / "static" / "css" / "reading-position-preview.css").read_text(encoding="utf-8")

    assert 'var(--panel-soft)' in css
    assert 'var(--border)' in css
    assert 'var(--accent)' in css
    assert ':focus-visible' in css
    assert 'white-space: pre-line' in css
    assert '@media (max-width: 480px)' in css


def test_grid_layout_is_resynced_when_series_grouping_moves_cards():
    """Series grouping relocates cards, so the grid class must be recomputed.

    `setExpanded` only ever fixes up the grid the button currently sits in.  When
    `applySeriesGrouping` moves an expanded card to another grid, the grid it left
    keeps `position-preview-expanded` and the grid it joined never gets it.
    """
    script = (ROOT / "static" / "js" / "reading-position-preview.js").read_text(encoding="utf-8")
    index = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")

    assert "window.bookbridgeSyncPreviewLayout = syncAllGridPreviewLayouts" in script
    assert "document.querySelectorAll('.book-grid').forEach(applyGridPreviewLayout)" in script

    # Scoped to the function body rather than a fixed character window, so the
    # assertion keeps meaning as applySeriesGrouping grows.
    start = index.index("function applySeriesGrouping")
    end = index.index("\n            function ", start + 1)
    assert "window.bookbridgeSyncPreviewLayout()" in index[start:end]
