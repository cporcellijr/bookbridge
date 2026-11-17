"""Demo BookBridge seeded with public-domain books, for the README and docs screenshots.

Serves the checkout's own code against a throwaway database in a temp folder; it
never touches /data or a live instance. Run it, then shoot.mjs in a second shell:

    .venv/Scripts/python.exe -I scripts/readme_screenshots/harness.py
    node scripts/readme_screenshots/shoot.mjs

Sign in as demo / demo. Covers are public-domain paintings, shrunk to 480px.
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parents[1]
COVERS = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "covers"
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 5799
sys.path.insert(0, str(WT))
sys.path.insert(0, str(WT / "tests"))
os.chdir(WT)

tmp = Path(tempfile.mkdtemp(prefix="bb-shots-"))
for sub in ("books", "epub-cache", "covers", "covers/thumbs"):
    (tmp / sub).mkdir()
os.environ.update(
    DATA_DIR=str(tmp), BOOKS_DIR=str(tmp / "books"),
    TEMPLATE_DIR=str(WT / "templates"), STATIC_DIR=str(WT / "static"),
)

from tests.test_webserver import MockContainer  # noqa: E402
from src.db.database_service import DatabaseService  # noqa: E402
from src.db.models import Book, State, StorygraphDetails  # noqa: E402
import src.db.migration_utils  # noqa: E402
from src.web_server import create_app  # noqa: E402

service = DatabaseService(str(tmp / "demo.db"))
user = service.create_user("demo", "demo", role="admin")
service.set_user_credential(user.id, "UI_MODE", "new")

# Settings and My Integrations shots: placeholder hosts and logins, never real ones.
os.environ.update(
    ABS_ENABLED="true", ABS_SERVER="http://audiobookshelf.local:13378",
    KOSYNC_ENABLED="true", BOOKORBIT_ENABLED="true", BOOKORBIT_SERVER="http://bookorbit.local:3000",
    STORYTELLER_ENABLED="true", STORYTELLER_API_URL="http://storyteller.local:8001",
    BOOKLORE_ENABLED="true", BOOKLORE_SERVER="http://grimmory.local:6060",
    KAVITA_ENABLED="true", KAVITA_SERVER="http://kavita.local:5000",
    CWA_ENABLED="true", CWA_SERVER="http://calibre-web.local:8083",
    BOOKFUSION_ENABLED="true", HARDCOVER_ENABLED="true", STORYGRAPH_ENABLED="true", GOODREADS_ENABLED="true",
)
for key, value in (("ABS_KEY", "demo-abs-api-key"), ("ABS_LIBRARY_ID", "demo-library-id"),
                   ("KOSYNC_USER", "demo"), ("KOSYNC_KEY", "demo-password"),
                   ("BOOKORBIT_USER", "demo"), ("BOOKORBIT_PASSWORD", "demo-password")):
    service.set_user_credential(user.id, key, value)

now = time.time()
H = 3600
# (slug, title, author, series, seq, mode, audio_source, states {client: pct}, leader, hours_ago, storyteller)
BOOKS = [
    ("pride-and-prejudice", "Pride and Prejudice", "Jane Austen", None, None, "audiobook", None,
     {"abs": .62, "bookorbit": .61, "kosync": .62}, "kosync", 1, False),
    ("dracula", "Dracula", "Bram Stoker", None, None, "audiobook", "BookOrbit",
     {"bookorbitaudio": .34, "bookorbit": .34, "storyteller": .34}, "bookorbitaudio", 3, True),
    ("a-study-in-scarlet", "A Study in Scarlet", "Arthur Conan Doyle", "Sherlock Holmes", 1, "audiobook", None,
     {"abs": 1.0, "kosync": 1.0}, "abs", 200, False),
    ("the-sign-of-the-four", "The Sign of the Four", "Arthur Conan Doyle", "Sherlock Holmes", 2, "audiobook", None,
     {"abs": .47, "kosync": .47, "storyteller": .47}, "storyteller", 20, True),
    ("the-adventures-of-sherlock-holmes", "The Adventures of Sherlock Holmes", "Arthur Conan Doyle", "Sherlock Holmes", 3,
     "audiobook", None, {}, None, 0, False),
    ("frankenstein", "Frankenstein", "Mary Shelley", None, None, "ebook_only", None,
     {"bookorbit": .18, "kosync": .18}, "kosync", 6, False),
    ("moby-dick", "Moby Dick", "Herman Melville", None, None, "audiobook", None,
     {"abs": .41, "kosync": .41, "bookorbit": .40}, "abs", 30, False),
    ("treasure-island", "Treasure Island", "Robert Louis Stevenson", None, None, "audiobook", "BookOrbit",
     {"bookorbitaudio": .88, "kosync": .88}, "bookorbitaudio", 2, False),
    ("the-time-machine", "The Time Machine", "H. G. Wells", None, None, "audiobook", None,
     {"abs": 1.0, "bookorbit": 1.0}, "abs", 400, False),
    ("the-war-of-the-worlds", "The War of the Worlds", "H. G. Wells", None, None, "audiobook", None,
     {"abs": .07, "kosync": .07}, "kosync", 50, False),
    ("jane-eyre", "Jane Eyre", "Charlotte Bronte", None, None, "audiobook", None,
     {"abs": .73, "kosync": .73, "bookorbit": .73}, "bookorbit", 9, False),
    ("the-wonderful-wizard-of-oz", "The Wonderful Wizard of Oz", "L. Frank Baum", "Oz", 1, "audiobook", None,
     {"abs": 1.0, "kosync": 1.0}, "kosync", 300, False),
    ("the-marvelous-land-of-oz", "The Marvelous Land of Oz", "L. Frank Baum", "Oz", 2, "audiobook", None,
     {"abs": .29, "kosync": .29}, "abs", 12, False),
    ("the-great-gatsby", "The Great Gatsby", "F. Scott Fitzgerald", None, None, "audiobook", None,
     {}, None, 0, False),
]

for i, (slug, title, author, series, seq, mode, audio_source, states, leader, hours, st) in enumerate(BOOKS):
    abs_id = f"demo-{i:02d}"
    doc = f"{i:02d}" + "d" * 30
    shutil.copy(COVERS / f"{slug}.jpg", tmp / "covers" / f"{doc}.jpg")
    shutil.copy(COVERS / f"{slug}.jpg", tmp / "covers" / "thumbs" / f"{doc}.jpg")
    service.save_book(Book(
        abs_id=abs_id, abs_title=title, status="active", sync_mode=mode,
        ebook_filename=f"{title} - {author}.epub", kosync_doc_id=doc, duration=11 * H,
        audio_source=audio_source, audio_source_id=abs_id if audio_source else None,
        series_name=series, series_sequence=seq,
        storyteller_uuid=f"00000000-0000-0000-0000-0000000000{i:02d}" if st else None,
        user_id=user.id,
    ))
    service.link_user_book(user.id, abs_id)
    service.save_storygraph_details(StorygraphDetails(abs_id=abs_id, storygraph_book_id=f"sg-{i}", storygraph_pages=[432, 418, 108, 160, 307, 280, 635, 292, 104, 192, 507, 154, 287, 180][i], storygraph_rating=[4.3, 4.0, 3.8, 3.9, 4.2, 3.9, 3.6, 3.9, 3.8, 3.8, 4.1, 3.9, 3.7, 3.8][i], matched_by="manual"))
    ts = now - hours * H
    for client, pct in states.items():
        service.save_state(State(
            abs_id=abs_id, client_name=client, percentage=pct, timestamp=pct * 11 * H,
            last_updated=ts, user_id=user.id,
        ))
    if leader:
        service.record_sync_marker(abs_id, leader, ts, user_id=user.id)

# Hero card extras: synopsis, a week of listening and KOReader reading, and every hero piece switched on.
service.set_user_credential(user.id, "UI_HERO_CONTENT", "synopsis,stats,streak")
service.set_user_book_synopsis_cache(user.id, "demo-00", (
    "Elizabeth Bennet, the sharp-witted second of five daughters, meets the proud and wealthy Mr. Darcy "
    "at a country ball and takes an instant dislike to him. As their paths keep crossing, first impressions "
    "give way to a slower, funnier and far more honest understanding of each other."), now)
DAY = 24 * H
for d in range(9):
    start = now - d * DAY - 9 * H
    service.record_reading_session("demo-00", "AUDIOBOOK", start, start + 2400 + 300 * (d % 3), 2400 + 300 * (d % 3),
                                   start_progress=.40 + .02 * (8 - d), end_progress=.42 + .02 * (8 - d),
                                   leader_client="ABS", user_id=user.id)
pages = []
for d in range(7):
    base = now - d * DAY - 2 * H
    for k in range(18 + 3 * (d % 2)):
        pages.append({"md5": "00" + "d" * 30, "page": 150 + d * 25 + k, "start_time": base + k * 70,
                      "duration": 65, "total_pages": 432})
service.bulk_insert_koreader_page_stats("Kobo Libra", "demo-kobo", pages, user_id=user.id)

import json  # noqa: E402
from unittest.mock import Mock  # noqa: E402

os.environ["DIAGNOSTICS_PROMPTED"] = "true"
# (slug, title, author, audio source key, score, reason, series, seq)
PAIRS = [
    ("emma", "Emma", "Jane Austen", "li_emma", 100, "exact", None, None),
    ("persuasion", "Persuasion", "Jane Austen", "bookorbit:persuasion", 100, "exact", None, None),
    ("sense-and-sensibility", "Sense and Sensibility", "Jane Austen", "li_sense", 100, "exact", None, None),
    ("the-hound-of-the-baskervilles", "The Hound of the Baskervilles", "Arthur Conan Doyle", "li_hound", 100, "exact",
     "Sherlock Holmes", 5),
    ("ozma-of-oz", "Ozma of Oz", "L. Frank Baum", "bookorbit:ozma", 100, "exact", "Oz", 3),
    ("heart-of-darkness", "Heart of Darkness", "Joseph Conrad", "li_heart", 100, "exact", None, None),
    ("kidnapped", "Kidnapped", "Robert Louis Stevenson", "li_kidnapped", 92, "fuzzy", None, None),
    ("the-invisible-man", "The Invisible Man", "H. G. Wells", "bookorbit:invisible", 84, "fuzzy", None, None),
]
cache = {}
for j, (slug, title, author, key, score, reason, series, seq) in enumerate(PAIRS):
    doc = f"9{j}" + "e" * 30
    shutil.copy(COVERS / f"{slug}.jpg", tmp / "covers" / f"{doc}.jpg")
    cache[key] = {
        "abs_id": key, "bridge_key": key, "audio_title": title, "audio_author": author,
        "audio_source_id": key, "audio_cover_url": f"/covers/{doc}.jpg",
        "series_name": series, "series_sequence": seq,
        "matches": [{
            "source": "BookOrbit" if j % 2 else "Calibre-Web", "source_id": f"{key}-e",
            "ebook_filename": f"{title} - {author}.epub", "display_name": title,
            "title": title, "author": author, "score": score, "match_reason": reason,
            "author_agrees": True, "title_key": title.lower(),
        }],
    }
# Discover's Ebook Only tab: ebooks the last scan found no audiobook for (an empty audio pool).
from src.services.ebook_discovery import build_discovery_snapshot, build_ebook_inventory  # noqa: E402
from src.services.suggestions_service import SuggestionsService  # noqa: E402

no_audio = build_discovery_snapshot(
    SuggestionsService(database_service=Mock(), container=Mock(), manager=Mock(),
                       get_audiobooks_conditionally=list, get_searchable_ebooks=lambda _q: [],
                       audiobook_matches_search=lambda _a, _q: False, get_abs_author=lambda _a: "", logger=Mock()),
    build_ebook_inventory([
        {"source": "BookOrbit", "source_id": f"eo-{k}", "title": title, "author": author,
         "series_name": series, "series_sequence": seq}
        for k, (title, author, series, seq) in enumerate([
            ("The Moonstone", "Wilkie Collins", None, None),
            ("Middlemarch", "George Eliot", None, None),
            ("The Valley of Fear", "Arthur Conan Doyle", "Sherlock Holmes", 4),
            ("Northanger Abbey", "Jane Austen", None, None),
            ("The Island of Doctor Moreau", "H. G. Wells", None, None),
        ])
    ]), [], now=now - 2 * H)
(tmp / f"suggestions_scan_cache_user_{user.id}.json").write_text(json.dumps({
    "scan_cache_by_abs": cache, "scan_cache_no_match_abs_ids": [], "scan_last_stats": {},
    "scan_generation": "demo-1", "scanned_at": now - 2 * H, "updated_at": now - 2 * H,
    "ebook_discovery": no_audio,
}), encoding="utf-8")

# Processing: one book aligning, two waiting their turn, one finished today.
from src.db.models import Job  # noqa: E402

for k, (title, author, status, progress) in enumerate([
    ("Little Women", "Louisa May Alcott", "processing", .46),
    ("The Call of the Wild", "Jack London", "pending", None),
    ("Around the World in Eighty Days", "Jules Verne", "pending", None),
    ("The Secret Garden", "Frances Hodgson Burnett", "active", 1.0),
]):
    abs_id = f"demo-job-{k}"
    service.save_book(Book(abs_id=abs_id, abs_title=title, status=status, sync_mode="audiobook",
                           ebook_filename=f"{title} - {author}.epub", duration=9 * H, user_id=user.id))
    service.link_user_book(user.id, abs_id)
    if progress is not None:
        service.save_job(Job(abs_id=abs_id, kind="alignment", progress=progress, last_attempt=now - (k + 1) * 600))

# Discover's Search tab: a small fixed catalogue instead of live providers. "persuasion" finds both
# halves; "northanger" finds only the ebook, so the page offers to get the audiobook.
from types import SimpleNamespace  # noqa: E402

import src.api.acquisition_routes  # noqa: E402
import src.web_server  # noqa: E402


def _demo_audio(title, author, source, sid, cover_slug):
    return SimpleNamespace(source=source, source_id=sid, title=title, subtitle="", series_label="", series_name=None,
                           series_sequence=None, authors=author, language="en", cover_url=f"/covers/{cover_slug}.jpg",
                           duration=8 * H, provider_book_id=sid, provider_file_id="")


def _demo_ebook(title, author, source, sid):
    return SimpleNamespace(name=f"{title} - {author}.epub", display_name=title, source_file_id=None, title=title,
                           subtitle="", authors=author, series_name=None, series_sequence=None, source=source,
                           source_id=sid, path=None, abs_identifier=None, language="en")


DEMO_SEARCH = {
    "persuasion": ([_demo_audio("Persuasion", "Jane Austen", "BookOrbit", "bookorbit:persuasion", "91" + "e" * 30)],
                   [_demo_ebook("Persuasion", "Jane Austen", "BookOrbit", "bo-persuasion"),
                    _demo_ebook("Persuasion", "Jane Austen", "Calibre-Web", "cw-persuasion")]),
    "northanger": ([], [_demo_ebook("Northanger Abbey", "Jane Austen", "BookOrbit", "eo-3")]),
}


def _demo_search_results(search, review, ebook_key, generation):
    term = (search or "").strip().lower()
    audio, ebooks = next((hit for key, hit in DEMO_SEARCH.items() if key in term), ([], []))
    return audio, ebooks, [], False, term, "search"


src.web_server._suggestions_search_results = _demo_search_results
src.api.acquisition_routes._bookorbit_client = lambda _web: SimpleNamespace(
    request_sources=lambda: {"enabled": 1}, can_pick_releases=lambda: False)
src.api.acquisition_routes._shelfmark_client = lambda _web: None

container = MockContainer()
container.mock_database_service = service
configured = Mock(is_configured=Mock(return_value=True))
demo_clients = {name: configured for name in ("ABS", "KoSync", "Storyteller", "BookOrbit", "BookOrbitAudio")}
container.sync_clients = lambda: demo_clients
container.mock_user_client_registry.get_clients.return_value.sync_clients = demo_clients
container.mock_user_client_registry.get_clients.return_value.abs_client = None
for name, folder in (("data_dir", ""), ("books_dir", "books"), ("epub_cache_dir", "epub-cache")):
    setattr(container, name, lambda p=tmp / folder: p)
src.db.migration_utils.initialize_database = lambda _d: service
app, _ = create_app(test_container=container)
app.config.update(TESTING=False, LOGIN_DISABLED=False)
print(f"DATA_DIR={tmp}", flush=True)
app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)
