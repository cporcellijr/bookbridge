"""Comics remain ebook-only across bulk, automatic and shared mapping paths."""

from unittest.mock import MagicMock

import src.web_server as web
from src.services.book_mapping_service import BookMappingService


def suggestion(*filenames):
    return {
        'abs_id': 'audio-1', 'audio_source': 'ABS', 'audio_source_id': 'audio-1',
        'matches': [{'ebook_filename': name, 'source': 'BookLore', 'source_id': '45',
                     'score': 100} for name in filenames],
    }


def test_bulk_comic_is_skipped_but_epub_alternative_is_queued():
    assert web._queue_item_from_suggestion(suggestion('comic.cbz')) is None
    item = web._queue_item_from_suggestion(suggestion('comic.cbz', 'book.epub'))
    assert item['ebook_filename'] == 'book.epub'
    assert item['audio_source'] == 'ABS'


def test_auto_match_skips_comic_and_uses_epub(monkeypatch):
    monkeypatch.setenv('SUGGESTIONS_AUTO_MATCH_ENABLED', 'true')
    monkeypatch.setenv('SUGGESTIONS_AUTO_MATCH_THRESHOLD', '100')
    container = MagicMock()
    monkeypatch.setattr(web, 'container', container)
    monkeypatch.setattr(web, '_shelve_saved_ebook', MagicMock())
    mapping = container.book_mapping_service()
    web._auto_match_suggestions({'suggestions': [suggestion('comic.cbz')]}, 7)
    mapping.create_audio_mapping_from_match.assert_not_called()
    web._auto_match_suggestions({'suggestions': [suggestion('comic.cbz', 'book.epub')]}, 7)
    assert mapping.create_audio_mapping_from_match.call_args.kwargs['ebook_filename'] == 'book.epub'


def test_shared_mapping_boundary_rejects_comic_before_any_work():
    service = BookMappingService(MagicMock(), MagicMock(), MagicMock(), MagicMock(), {})
    service._compute_kosync_id = MagicMock(return_value='hash')
    assert service.create_audio_mapping_from_match(
        audio_source='ABS', audio_source_id='audio-1', audio_title='Comic',
        ebook_filename='comic.CBZ', user_id=7,
    ) is None
    service._compute_kosync_id.assert_not_called()
    service.database_service.save_book.assert_not_called()
