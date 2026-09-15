"""Repointing a book's audiobook provider must not cost a re-match.

Moving a library from Audiobookshelf into BookOrbit changes who serves the audio,
not which ebook the book is paired with. Re-matching would rebuild the mapping
under a new `bookorbit:<id>` primary key and orphan every State row, alignment
map, KOSync link and annotation attached to the old `abs_id`. The repoint instead
updates `audio_source`/`audio_source_id` in place, which is enough because
`ABSSyncClient.supports_book` returns `(audio_source or "ABS") == "ABS"` and
`BookOrbitAudioSyncClient._resolve_book_id` reads `audio_source_id` — neither
looks at `abs_id`.

Duration is the safety property. The alignment map was built against the old
audio and stays valid only if the new file is the same recording, so a candidate
whose run time disagrees is never applied automatically.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.audio_repoint_service import AudioRepointService

ABS_ID = "30d4addf-7ba8-4295-abc9-ee512e042336"


def _book(abs_id=ABS_ID, title="Four Nights in May", duration=34016.0,
          audio_source="ABS", status="active", sync_mode="audiobook"):
    return SimpleNamespace(
        abs_id=abs_id, abs_title=title, duration=duration,
        audio_source=audio_source, audio_source_id=abs_id,
        status=status, sync_mode=sync_mode,
    )


def _catalog_entry(book_id, title, kind="audiobook"):
    return {"id": book_id, "title": title, "kind": kind, "kinds": [kind]}


class _FakeDb:
    def __init__(self, books):
        self._books = {b.abs_id: b for b in books}
        self.updates = []
        self.links = []
        self.claimants = {}

    def get_all_books(self):
        return list(self._books.values())

    def get_book(self, abs_id):
        return self._books.get(abs_id)

    def get_book_user_ids(self, abs_id):
        return list(self.claimants.get(abs_id, [1]))

    def set_user_bookorbit_link(self, user_id, abs_id, ebook_id=None, audio_id=None,
                                title=None, author=None):
        self.links.append({"user_id": user_id, "abs_id": abs_id, "audio_id": audio_id})
        return self.links[-1]

    def update_book_fields(self, abs_id, **fields):
        self.updates.append((abs_id, fields))
        book = self._books.get(abs_id)
        if book is None:
            return False
        for key, value in fields.items():
            setattr(book, key, value)
        return True


def _client(catalog, durations):
    client = MagicMock()
    client.is_configured.return_value = True
    client.get_all_books.return_value = catalog
    client._info_offers_kind.side_effect = lambda info, kind: kind in (info.get("kinds") or [])
    client.get_audiobook_info.side_effect = lambda bid: (
        {"duration_seconds": durations.get(int(bid) if str(bid).isdigit() else bid),
         "primary_file_id": 900}
    )
    return client


class TestTitleNormalisation(unittest.TestCase):
    def test_strips_edition_noise_and_series_number(self):
        n = AudioRepointService.normalize_title
        self.assertEqual(n("01 Sandman Slim"), n("Sandman Slim"))
        self.assertEqual(n("Dragon's Justice 7 (Unabridged)"), n("Dragon's Justice 7"))
        self.assertEqual(n("Home Maker - Erin Cole"), n("Home Maker"))
        self.assertEqual(n("Bedlam: Book One of the Sheol Saga"), n("Bedlam"))

    def test_durations_agree_within_tolerance(self):
        agree = AudioRepointService.durations_agree
        self.assertTrue(agree(34016.0, 34016.0))
        self.assertTrue(agree(34016.0, 34050.0))     # inside the 2% band
        self.assertFalse(agree(34016.0, 40000.0))    # a different narration
        self.assertFalse(agree(None, 34016.0))
        self.assertFalse(agree(34016.0, None))


class TestPlan(unittest.TestCase):
    def test_unique_title_with_matching_duration_is_automatic(self):
        db = _FakeDb([_book()])
        svc = AudioRepointService(db, _client([_catalog_entry(5568, "Four Nights in May")], {5568: 34016.0}))

        plan = svc.build_plan()

        self.assertEqual(plan["counts"], {"total": 1, "auto": 1, "review": 0, "unmatched": 0})
        self.assertEqual(plan["auto"][0]["target"]["id"], 5568)

    def test_duration_mismatch_is_never_automatic(self):
        """A same-titled book of a different length is a different narration; the
        existing alignment would not fit it."""
        db = _FakeDb([_book()])
        svc = AudioRepointService(db, _client([_catalog_entry(99, "Four Nights in May")], {99: 61000.0}))

        plan = svc.build_plan()

        self.assertEqual(plan["counts"]["auto"], 0)
        self.assertEqual(plan["counts"]["review"], 1)
        self.assertIn("different narration", plan["review"][0]["reason"])

    def test_duplicate_copies_go_to_review_not_a_guess(self):
        db = _FakeDb([_book(title="Frostvale Hollow", duration=50000.0)])
        catalog = [_catalog_entry(1, "Frostvale Hollow"), _catalog_entry(2, "Frostvale Hollow")]
        svc = AudioRepointService(db, _client(catalog, {1: 50000.0, 2: 50000.0}))

        plan = svc.build_plan()

        self.assertEqual(plan["counts"]["review"], 1)
        self.assertEqual(len(plan["review"][0]["candidates"]), 2)

    def test_book_absent_from_bookorbit_is_unmatched(self):
        db = _FakeDb([_book(title="Dungeon Runner Dana")])
        svc = AudioRepointService(db, _client([_catalog_entry(1, "Something Else Entirely")], {1: 100.0}))

        plan = svc.build_plan()

        self.assertEqual(plan["counts"]["unmatched"], 1)
        self.assertEqual(plan["counts"]["auto"], 0)

    def test_ebook_only_and_inactive_books_are_never_considered(self):
        """Ebook-only rows carry audio_source NULL — they have no audiobook."""
        db = _FakeDb([
            _book(abs_id="ebook-1", audio_source=None, sync_mode="ebook_only"),
            _book(abs_id="done-1", status="complete"),
            _book(abs_id="already", audio_source="BookOrbit"),
        ])
        svc = AudioRepointService(db, _client([], {}))

        self.assertEqual(svc.build_plan()["counts"]["total"], 0)

    def test_no_catalog_leaves_everything_unmatched(self):
        db = _FakeDb([_book()])
        client = MagicMock()
        client.is_configured.return_value = False
        plan = AudioRepointService(db, client).build_plan()
        self.assertEqual(plan["counts"]["unmatched"], 1)


class TestApply(unittest.TestCase):
    def test_apply_changes_only_audio_fields_and_keeps_the_primary_key(self):
        """abs_id must survive — every State row, alignment and link hangs off it."""
        book = _book()
        db = _FakeDb([book])
        svc = AudioRepointService(db, _client([_catalog_entry(5568, "Four Nights in May")], {5568: 34016.0}))

        result = svc.apply([{"abs_id": ABS_ID, "target_id": 5568}])

        self.assertEqual(result["updated"], 1)
        self.assertEqual(book.abs_id, ABS_ID)
        self.assertEqual(book.audio_source, "BookOrbit")
        self.assertEqual(book.audio_source_id, "5568")
        self.assertEqual(book.audio_provider_book_id, "5568")
        changed = db.updates[0][1]
        self.assertNotIn("abs_id", changed)
        self.assertNotIn("ebook_filename", changed)
        self.assertNotIn("transcript_file", changed)
        self.assertNotIn("kosync_doc_id", changed)

    def test_apply_skips_a_book_that_is_no_longer_abs(self):
        db = _FakeDb([_book(audio_source="BookOrbit")])
        svc = AudioRepointService(db, _client([], {}))

        result = svc.apply([{"abs_id": ABS_ID, "target_id": 5568}])

        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "audio source is no longer ABS")

    def test_apply_ignores_incomplete_selections(self):
        db = _FakeDb([_book()])
        svc = AudioRepointService(db, _client([], {}))

        result = svc.apply([{"abs_id": ABS_ID}, {"target_id": 1}])

        self.assertEqual(result["updated"], 0)
        self.assertEqual(len(result["skipped"]), 2)

    def test_apply_points_each_claimants_bookorbit_link_at_the_audiobook(self):
        """These books already carry an ebook-only UserBookOrbitLink. Because
        resolve_bookorbit_audio_id returns as soon as it finds a link, leaving
        audio_id unset makes supports_book reject the book and no audio syncs —
        the shared Book.audio_source_id fallback is never reached."""
        db = _FakeDb([_book()])
        db.claimants[ABS_ID] = [1, 2]
        svc = AudioRepointService(db, _client([_catalog_entry(5568, "Four Nights in May")], {5568: 34016.0}))

        svc.apply([{"abs_id": ABS_ID, "target_id": 5568}])

        self.assertEqual(
            sorted((l["user_id"], l["audio_id"]) for l in db.links),
            [(1, "5568"), (2, "5568")],
        )

    def test_apply_writes_no_link_when_nothing_was_repointed(self):
        db = _FakeDb([_book(audio_source="BookOrbit")])
        svc = AudioRepointService(db, _client([], {}))

        svc.apply([{"abs_id": ABS_ID, "target_id": 5568}])

        self.assertEqual(db.links, [])


class TestUndo(unittest.TestCase):
    def test_undo_restores_the_abs_id_as_the_audio_source_id(self):
        """No backup table needed: the ABS item id is still the primary key."""
        book = _book(audio_source="BookOrbit")
        book.audio_source_id = "5568"
        db = _FakeDb([book])

        result = AudioRepointService(db, _client([], {})).undo()

        self.assertEqual(result["restored"], 1)
        self.assertEqual(book.audio_source, "ABS")
        self.assertEqual(book.audio_source_id, ABS_ID)

    def test_undo_never_touches_a_natively_bookorbit_book(self):
        """A book matched to BookOrbit audio from the start has no ABS id to go back to."""
        native = _book(abs_id="bookorbit:5568", audio_source="BookOrbit")
        db = _FakeDb([native])

        result = AudioRepointService(db, _client([], {})).undo()

        self.assertEqual(result["restored"], 0)
        self.assertEqual(native.audio_source, "BookOrbit")

    def test_undo_can_be_scoped_to_named_books(self):
        a = _book(abs_id="a", audio_source="BookOrbit")
        b = _book(abs_id="b", audio_source="BookOrbit")
        db = _FakeDb([a, b])

        AudioRepointService(db, _client([], {})).undo(["a"])

        self.assertEqual(a.audio_source, "ABS")
        self.assertEqual(b.audio_source, "BookOrbit")


if __name__ == "__main__":
    unittest.main()
