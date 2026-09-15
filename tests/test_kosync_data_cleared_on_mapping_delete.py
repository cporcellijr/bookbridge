#!/usr/bin/env python3
"""
Regression tests for issue #358 (second cause).

The reporter deleted his Dungeon Runner Dana mapping and re-matched it, and the
book came straight back at 100%. Mapping cleanup only removed the KoSync document
for `ebook_only` mappings, so an audiobook mapping left both the `KosyncDocument`
row and every `KosyncUserProgress` row behind. The document hash is derived from
the EPUB's content, so re-matching the same file re-links the identical hash
(6453d1af20fab7ae4ccd9d1b6f52b09a appeared before and after his re-match), the
fresh book has no State rows, and the furthest-wins gate in
`_respond_from_book_states` hands the pre-delete 100% straight back:

    sibling_pct (1.0) > synced_pct (0.0)  ->  return 100%

So deleting and re-matching could not clear a stuck position — it guaranteed it
came back.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.database_service import DatabaseService
from src.db.models import Book, KosyncDocument, KosyncUserProgress, User

DOC_HASH = '6453d1af20fab7ae4ccd9d1b6f52b09a'
SIBLING_HASH = 'aa11bb22cc33dd44ee55ff6677889900'


class TestKosyncDataClearedOnMappingDelete(unittest.TestCase):
    """KoSync progress must not outlive the mapping it belonged to."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = DatabaseService(str(Path(self.temp_dir) / 'test.db'))
        with self.db.get_session() as session:
            session.add(User(username='reader', password_hash='x', role='admin'))
        with self.db.get_session() as session:
            self.user_id = session.query(User).first().id

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed_finished_audiobook(self, sync_mode='audiobook'):
        """A finished audiobook mapping: linked doc + per-user progress at 100%."""
        book = Book(
            abs_id='dcc-abs-id',
            abs_title='Dungeon Runner Dana',
            ebook_filename='dcc.epub',
            kosync_doc_id=DOC_HASH,
            sync_mode=sync_mode,
        )
        self.db.save_book(book)
        with self.db.get_session() as session:
            for doc_hash in (DOC_HASH, SIBLING_HASH):
                session.add(KosyncDocument(
                    document_hash=doc_hash,
                    progress='/body/DocFragment[42]/body/p[7]',
                    percentage=1.0,
                    device='koreader',
                    device_id='koreader',
                ))
                session.add(KosyncUserProgress(
                    document_hash=doc_hash,
                    user_id=self.user_id,
                    progress='/body/DocFragment[42]/body/p[7]',
                    percentage=1.0,
                    device='koreader',
                    device_id='koreader',
                ))
        with self.db.get_session() as session:
            for doc in session.query(KosyncDocument).all():
                doc.linked_abs_id = 'dcc-abs-id'
        return book

    def _remaining(self):
        with self.db.get_session() as session:
            docs = session.query(KosyncDocument).filter(
                KosyncDocument.document_hash.in_([DOC_HASH, SIBLING_HASH])
            ).count()
            progress = session.query(KosyncUserProgress).filter(
                KosyncUserProgress.document_hash.in_([DOC_HASH, SIBLING_HASH])
            ).count()
        return docs, progress

    def test_audiobook_mapping_clears_documents_and_progress(self):
        self._seed_finished_audiobook()
        self.assertEqual(self._remaining(), (2, 2), "seed did not take")

        docs_deleted, progress_deleted = self.db.delete_kosync_data_for_book('dcc-abs-id')

        self.assertEqual((docs_deleted, progress_deleted), (2, 2))
        self.assertEqual(
            self._remaining(), (0, 0),
            "KoSync progress survived deletion of an audiobook mapping",
        )

    def test_ebook_only_mapping_still_cleared(self):
        self._seed_finished_audiobook(sync_mode='ebook_only')

        self.db.delete_kosync_data_for_book('dcc-abs-id')

        self.assertEqual(self._remaining(), (0, 0))

    def test_rematch_after_delete_finds_no_stale_progress(self):
        """The reporter's exact sequence: delete, then re-match the same EPUB."""
        self._seed_finished_audiobook()
        self.db.delete_kosync_data_for_book('dcc-abs-id')

        # Re-matching the same file yields the same content hash and re-links it.
        rematched = Book(
            abs_id='dcc-abs-id',
            abs_title='Dungeon Runner Dana',
            ebook_filename='dcc.epub',
            kosync_doc_id=DOC_HASH,
            sync_mode='audiobook',
        )
        self.db.save_book(rematched)

        stale = self.db.get_user_kosync_progress_for_book('dcc-abs-id', self.user_id)
        self.assertEqual(
            [row for row in stale if row.percentage and float(row.percentage) > 0],
            [],
            "A re-matched book still sees the pre-delete 100% position",
        )

    def test_other_books_are_untouched(self):
        self._seed_finished_audiobook()
        other_hash = 'ffffffffffffffffffffffffffffffff'
        self.db.save_book(Book(
            abs_id='other-abs-id',
            abs_title='Some Other Book',
            ebook_filename='other.epub',
            kosync_doc_id=other_hash,
        ))
        with self.db.get_session() as session:
            session.add(KosyncDocument(
                document_hash=other_hash,
                progress='/body/DocFragment[1]/body/p[1]',
                percentage=0.4,
                linked_abs_id='other-abs-id',
            ))
            session.add(KosyncUserProgress(
                document_hash=other_hash,
                user_id=self.user_id,
                progress='/body/DocFragment[1]/body/p[1]',
                percentage=0.4,
            ))

        self.db.delete_kosync_data_for_book('dcc-abs-id')

        with self.db.get_session() as session:
            self.assertEqual(
                session.query(KosyncDocument).filter(
                    KosyncDocument.document_hash == other_hash
                ).count(), 1,
                "Deleting one mapping removed another book's KoSync document",
            )
            self.assertEqual(
                session.query(KosyncUserProgress).filter(
                    KosyncUserProgress.document_hash == other_hash
                ).count(), 1,
                "Deleting one mapping removed another book's KoSync progress",
            )

    def test_unknown_book_is_a_no_op(self):
        self.assertEqual(self.db.delete_kosync_data_for_book('nope'), (0, 0))
        self.assertEqual(self.db.delete_kosync_data_for_book(''), (0, 0))


if __name__ == '__main__':
    unittest.main(verbosity=2)
