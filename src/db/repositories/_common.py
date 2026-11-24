import unicodedata



_ANY_USER = object()

MANIFEST_RELEVANT_BOOK_FIELDS = (
    "status",
    "abs_title",
    "original_ebook_filename",
    "ebook_filename",
    "kosync_doc_id",
    "sync_mode",
    "ebook_source",
    "ebook_source_id",
    "ebook_source_file_id",
)

def _manifest_signature(book) -> tuple:
    """Snapshot the manifest-relevant columns of a book row for change detection."""
    return tuple(getattr(book, field, None) for field in MANIFEST_RELEVANT_BOOK_FIELDS)

_SQL_IN_CHUNK = 500

def _username_key(name: str) -> str:
    """Unicode-correct comparison key for usernames (SQLite lower() folds ASCII only)."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", (name or "").strip()).casefold())
