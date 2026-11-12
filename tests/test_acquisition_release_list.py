"""Release picker: the source name must be visible even with a single source."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).parent.parent / "templates" / "next" / "_acquisition_scripts.html"

HARNESS = """
const els = {};
const $ = (id) => els[id] || (els[id] = { innerHTML: '', hidden: false });
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const bbFormat = (t) => t;
const ACQ = { kinds: { ebook: 'Ebook' }, by: 'by', get: 'Get' };
%s
const releases = %s;
renderReleaseList(releases, () => {});
console.log(JSON.stringify({ rows: els.acqReleases.innerHTML, tabs: els.acqReleaseTabs.innerHTML, tabsHidden: els.acqReleaseTabs.hidden }));
"""


def _render(releases):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js not installed")
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("function renderReleaseList")
    fn = text[start:text.index("function resetAcqReleases")]
    out = subprocess.run([node, "-e", HARNESS % (fn, json.dumps(releases))],
                         capture_output=True, text=True, timeout=15)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout)


def _rel(key, group, label, source="", kind="ebook"):
    return {"key": key, "title": "T" + key, "kind": kind, "group": group,
            "group_label": label, "source": source}


def test_single_source_label_shown_on_rows():
    out = _render([_rel("1", "shelfmark", "Shelfmark"), _rel("2", "shelfmark", "Shelfmark")])
    assert out["tabsHidden"] is True
    assert out["rows"].count('<span class="acq-note">Shelfmark</span>') == 2


def test_single_source_still_shows_distinct_indexer():
    out = _render([_rel("1", "shelfmark", "Shelfmark", "MyIndexer")])
    assert '<span class="acq-note">Shelfmark</span>' in out["rows"]
    assert '<span class="acq-note">MyIndexer</span>' in out["rows"]


def test_multiple_sources_use_tabs_and_rows_do_not_repeat_label():
    out = _render([_rel("1", "bookorbit", "BookOrbit"), _rel("2", "shelfmark", "Shelfmark", "MyIndexer")])
    assert out["tabsHidden"] is False
    assert "BookOrbit" in out["tabs"] and "Shelfmark" in out["tabs"]
    assert '<span class="acq-note">BookOrbit</span>' not in out["rows"]
    out = _render([_rel("1", "bookorbit", "BookOrbit"), _rel("2", "shelfmark", "Shelfmark", "MyIndexer")])
    assert '<span class="acq-note">Shelfmark</span>' not in out["rows"]


def test_multiple_sources_show_distinct_indexer():
    out = _render([_rel("1", "bookorbit", "BookOrbit"), _rel("2", "shelfmark", "Shelfmark", "MyIndexer")])
    # active tab is the first group; switch by rendering with Shelfmark first
    out = _render([_rel("2", "shelfmark", "Shelfmark", "MyIndexer"), _rel("1", "bookorbit", "BookOrbit")])
    assert '<span class="acq-note">MyIndexer</span>' in out["rows"]


def test_source_equal_to_group_code_is_not_shown_raw():
    out = _render([_rel("1", "direct_download", "Direct download", "direct_download")])
    assert "direct_download</span>" not in out["rows"]
    assert '<span class="acq-note">Direct download</span>' in out["rows"]


def test_release_without_group_shows_no_source_label():
    out = _render([{"key": "1", "title": "T", "kind": "ebook", "source": "Idx"}])
    assert out["rows"].count('class="acq-note"') == 1 and "Idx" in out["rows"]
