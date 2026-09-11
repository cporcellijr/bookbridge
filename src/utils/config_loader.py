
import os
import logging
from src.db.database_service import DatabaseService

logger = logging.getLogger(__name__)

# Accepted truthy spellings for boolean settings. The settings UI checkbox
# posts "on", env files commonly use "1"/"yes", and our defaults use
# "true"/"false" — treat them all as enabled so a setting can't silently
# no-op just because of how it was spelled.
_TRUTHY = ("true", "1", "yes", "on")


def env_truthy(key: str, default: str = "false") -> bool:
    """Return whether an env/setting value is enabled, accepting any of
    true/1/yes/on (case-insensitive). Use this for every boolean setting read
    instead of comparing to "true" directly."""
    return os.environ.get(key, default).strip().lower() in _TRUTHY

# Full list of settings to manage
ALL_SETTINGS = [
    # Required ABS
    'ABS_ENABLED', 'ABS_SERVER', 'ABS_WEB_URL', 'ABS_KEY', 'ABS_LIBRARY_ID',
    
    # Optional ABS
    'ABS_COLLECTION_NAME', 'ABS_PROGRESS_OFFSET_SECONDS', 'ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID',
    'ABS_SOCKET_ENABLED', 'ABS_SOCKET_DEBOUNCE_SECONDS',
    
    'REMOTE_AUTH_ENABLED', 'REMOTE_AUTH_HEADER', 'REMOTE_AUTH_TRUSTED_PROXIES',

    # KOSync
    'KOSYNC_ENABLED', 'KOSYNC_SERVER', 'KOSYNC_USER', 'KOSYNC_KEY', 'KOSYNC_AUTH_METHOD',
    'KOSYNC_HASH_METHOD', 'KOSYNC_USE_PERCENTAGE_FROM_SERVER',
    'KOSYNC_RECENT_EXTERNAL_PUT_SECONDS', 'KOSYNC_AUTO_MAP_ON_AGREEMENT',
    'KOSYNC_HASH_RECONCILE_ENABLED', 'KOSYNC_HASH_RECONCILE_MINUTES',
    'KOSYNC_XPATH_ORDER_ENABLED', 'KOSYNC_FURTHEST_WINS',
    'KOSYNC_PUT_DEBOUNCE_SECONDS',
    'KOSYNC_BOOKORBIT_DISCOVERY_LIMIT',
    'KOREADER_COMBINE_DEVICE_STATS',
    'KOREADER_ANNOTATION_SYNC',

    # Storyteller
    'STORYTELLER_ENABLED', 'STORYTELLER_API_URL', 'STORYTELLER_USER', 'STORYTELLER_PASSWORD',
    
    # Grimmory
    'BOOKLORE_ENABLED', 'BOOKLORE_SERVER', 'BOOKLORE_WEB_URL', 'BOOKLORE_USER', 'BOOKLORE_PASSWORD', 'BOOKLORE_SHELF_NAME', 'BOOKLORE_LIBRARY_ID',
    'GRIMMORY_READING_SESSIONS', 'BOOKLORE_ANNOTATION_SYNC', 'BOOKLORE_ANNOTATION_SYNC_MINUTES',
    'DEVICE_SYNC_COLLECTION_SOURCE', 'DEVICE_SYNC_COLLECTIONS',
    'DEVICE_SYNC_EXCLUDED_SHELVES', 'DEVICE_SYNC_HARDCOVER_LISTS',
    'DEVICE_SYNC_HARDCOVER_LIST_NAMES', 'DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES',
    'BOOKLORE_SHELF_WATCH_ENABLED', 'BOOKLORE_SHELF_WATCH_NAME',
    'BOOKLORE_SHELF_WATCH_THRESHOLD', 'BOOKLORE_SHELF_WATCH_RESCAN_HOURS',

    # BookOrbit
    'BOOKORBIT_ENABLED', 'BOOKORBIT_SERVER', 'BOOKORBIT_WEB_URL', 'BOOKORBIT_USER', 'BOOKORBIT_PASSWORD',
    'BOOKORBIT_SHELF_NAME', 'BOOKORBIT_POLL_MODE', 'BOOKORBIT_POLL_SECONDS',
    'BOOKORBIT_POLL_WAIT_FOR_SETTLE',
    'BOOKORBIT_AUDIO_POLL_MODE', 'BOOKORBIT_AUDIO_POLL_SECONDS',
    'BOOKORBIT_AUDIO_POLL_WAIT_FOR_SETTLE',
    'BOOKORBIT_READING_SESSIONS',
    'BOOKORBIT_SHELF_WATCH_ENABLED', 'BOOKORBIT_SHELF_WATCH_NAME',
    'BOOKORBIT_SHELF_WATCH_THRESHOLD', 'BOOKORBIT_SHELF_WATCH_RESCAN_HOURS',
    'BOOKORBIT_ANNOTATION_SYNC_MINUTES', 'BOOKORBIT_KOSYNC_OWNER',

    # Kavita
    'KAVITA_ENABLED', 'KAVITA_SERVER', 'KAVITA_WEB_URL', 'KAVITA_API_KEY',
    'KAVITA_LIBRARY_ID', 'KAVITA_COLLECTION_NAME',
    'KAVITA_POLL_MODE', 'KAVITA_POLL_SECONDS', 'KAVITA_POLL_WAIT_FOR_SETTLE',
    'KAVITA_SHELF_WATCH_ENABLED', 'KAVITA_SHELF_WATCH_NAME',
    'KAVITA_SHELF_WATCH_THRESHOLD', 'KAVITA_SHELF_WATCH_RESCAN_HOURS',

    # BookFusion
    'BOOKFUSION_ENABLED', 'BOOKFUSION_API_URL', 'BOOKFUSION_ACCESS_TOKEN',
    'BOOKFUSION_API_KEY',
    'BOOKFUSION_ANNOTATION_SYNC', 'BOOKFUSION_POLL_MODE', 'BOOKFUSION_POLL_SECONDS',
    'BOOKFUSION_POLL_WAIT_FOR_SETTLE',

    # CWA (Calibre-Web Automated)
    'CWA_ENABLED', 'CWA_SERVER', 'CWA_WEB_URL', 'CWA_USERNAME', 'CWA_PASSWORD',
    'CWA_SYNC_ENABLED', 'CWA_SYNC_TOKEN', 'CWA_KOBO_SPAN_SYNC',
    'CWA_SYNC_POLL_MODE', 'CWA_SYNC_POLL_SECONDS', 'CWA_SYNC_POLL_WAIT_FOR_SETTLE',
    'CALIBRE_USE_ABS_IDENTIFIER', 'CALIBRE_LIBRARY_PATH',

    # Readest annotation sync (account is per-user; these are global engine defaults)
    'READEST_ENABLED', 'READEST_ANNOTATION_SYNC', 'READEST_ANNOTATION_SYNC_MINUTES',
    'READEST_EMAIL', 'READEST_PASSWORD',
    'READEST_ACCESS_TOKEN', 'READEST_REFRESH_TOKEN', 'READEST_TOKEN_EXPIRES_AT',
    'READEST_SUPABASE_URL', 'READEST_SUPABASE_ANON_KEY',
    'READEST_UPLOAD_ON_MATCH', 'READEST_GROUP_NAME',
    'READEST_UPLOAD_READING', 'READEST_UPLOAD_MAX_PER_RUN',
    'READEST_UPLOAD_SWEEP_MINUTES',

    # Progress Tracker
    # Hardcover
    'HARDCOVER_ENABLED', 'HARDCOVER_TOKEN', 'HARDCOVER_UPDATE_COOLDOWN_MINS',
    'HARDCOVER_ANNOTATION_SYNC', 'HARDCOVER_ANNOTATION_SYNC_MINUTES',
    'HARDCOVER_GRIMMORY_LIST_SYNC', 'HARDCOVER_GRIMMORY_LIST_PREFIX',
    'HARDCOVER_GRIMMORY_LIST_EXCLUDED_SHELVES',
    
    # StoryGraph
    'STORYGRAPH_ENABLED', 'STORYGRAPH_SESSION_COOKIE', 'STORYGRAPH_REMEMBER_USER_TOKEN',
    'STORYGRAPH_UPDATE_COOLDOWN_MINS',
    
    # LLM providers (Ollama, OpenAI, OpenAI-compatible local servers)
    'LLM_PROVIDER', 'LLM_BASE_URL', 'LLM_API_KEY',
    'LLM_EMBED_MODEL', 'LLM_CHAT_MODEL', 'LLM_NUM_CTX',

    # Ollama (legacy/local LLM settings, still honored)
    'OLLAMA_ENABLED', 'OLLAMA_URL', 'OLLAMA_EMBED_MODEL', 'OLLAMA_CHAT_MODEL',
    'OLLAMA_KEEP_ALIVE', 'OLLAMA_NUM_CTX',
    'OLLAMA_RERANK_SUGGESTIONS', 'OLLAMA_RERANK_BAND_MIN', 'OLLAMA_RERANK_BAND_MAX',
    'OLLAMA_JUDGE_SUGGESTIONS', 'OLLAMA_JUDGE_MARGIN', 'OLLAMA_JUDGE_CONFIDENCE_MIN',
    'OLLAMA_ALIGN_FALLBACK', 'OLLAMA_ALIGN_SIM_THRESHOLD',
    'OLLAMA_EBOOK_TEXT_FALLBACK',
    'OLLAMA_ALIGN_ANCHOR_RESCUE', 'OLLAMA_ALIGN_MAX_WINDOWS',
    'OLLAMA_ALIGN_CONTENT_GUARD', 'OLLAMA_ALIGN_CONTENT_MIN_SIM',
    'OLLAMA_SUGGEST_JUDGE_GATE', 'OLLAMA_SUGGEST_AUTOKEEP_SCORE',
    'OLLAMA_TRACKER_MATCH', 'OLLAMA_LIBRARY_MATCH',

    # One-time boot markers (no UI field; never in bool_keys)
    'SERVICE_GATE_RECONCILED',

    # Diagnostics
    'DIAGNOSTICS_OPT_IN', 'DIAGNOSTICS_PROMPTED', 'DIAGNOSTICS_INSTANCE_ID',
    'DIAGNOSTICS_ENDPOINT_URL', 'DIAGNOSTICS_LAST_SENT', 'DIAGNOSTICS_INGEST_TOKEN',

    # Telegram
    'TELEGRAM_ENABLED', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'TELEGRAM_LOG_LEVEL',
    
    # Shelfmark
    'SHELFMARK_URL', 'SHELFMARK_ENABLED',
    
    # Sync Behavior
    'SYNC_PERIOD_MINS', 'SYNC_DELTA_ABS_SECONDS', 'SYNC_DELTA_KOSYNC_PERCENT',
    'SYNC_DELTA_BETWEEN_CLIENTS_PERCENT', 'SYNC_DELTA_KOSYNC_WORDS',
    'SYNC_FRESHNESS_GUARDS', 'SYNC_COMPLETION_PROPAGATION', 'SYNC_COMPLETION_THRESHOLD',
    'SYNC_ROLLBACK_VETO_SECONDS', 'LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS',
    'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT', 'SYNC_ABS_EBOOK', 'ABS_EBOOK_LOCATOR_FORMAT',
    'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT',
    'FUZZY_MATCH_THRESHOLD', 'SUGGESTIONS_ENABLED',
    'SUGGESTIONS_AUTO_MATCH_ENABLED', 'SUGGESTIONS_AUTO_MATCH_THRESHOLD',
    'INSTANT_SYNC_ENABLED', 'KOREADER_SESSION_GAP_MINUTES', 'READING_SESSION_MERGE_MINUTES',
    'STORYTELLER_POLL_MODE', 'STORYTELLER_POLL_SECONDS', 'STORYTELLER_POLL_WAIT_FOR_SETTLE',
    'STORYTELLER_LISTENING_SESSIONS',
    'BOOKLORE_POLL_MODE', 'BOOKLORE_POLL_SECONDS', 'BOOKLORE_POLL_WAIT_FOR_SETTLE',
    'BOOKLORE_AUDIO_POLL_MODE', 'BOOKLORE_AUDIO_POLL_SECONDS',
    'BOOKLORE_AUDIO_POLL_WAIT_FOR_SETTLE',
    
    # System
    'TZ', 'LOG_LEVEL', 'DATA_DIR', 'BOOKS_DIR', 'EXTRA_EBOOK_DIRS',
    'AUDIOBOOKS_DIR', 'STORYTELLER_LIBRARY_DIR', 'STORYTELLER_ASSETS_DIR', 'STORYTELLER_UPLOAD_CHUNK_SIZE',
    'STORYTELLER_NO_EPUB_CACHE',
    'STORYTELLER_RECOVERY_MAX_WAIT_MINUTES', 'STORYTELLER_RECOVERY_POLL_INTERVAL_MINUTES',
    'EBOOK_CACHE_SIZE', 'ALIGNMENT_CACHE_SIZE',
    'JOB_MAX_RETRIES', 'JOB_RETRY_DELAY_MINS', 'WHISPER_MODEL',
    'WHISPER_DEVICE', 'WHISPER_COMPUTE_TYPE',
    'TRANSCRIPTION_PROVIDER', 'DEEPGRAM_API_KEY', 'DEEPGRAM_MODEL', 'WHISPER_CPP_URL', 'WHISPER_CPP_TIMEOUT', 'WHISPER_CPP_SEND_ORIGINAL', 'WHISPER_CPP_CHUNK_MINUTES',
    'CTC_ENABLED', 'CTC_MODEL', 'CTC_DEVICE',
    'ALIGNMENT_SEGMENTED_MAPS',
    'CONTENT_MATCH_GUARD', 'CONTENT_MATCH_MIN_OVERLAP',
    'AUDIO_SPLIT_DURATION_MINUTES',
    'SMIL_VALIDATION_THRESHOLD', 'TRANSCRIPT_MIN_COVERAGE',
    'DIAGNOSTICS_MAX_PAYLOAD_BYTES',
    'SHARE_ALL_BOOKS_WITH_ALL_USERS',
]

# Default values
DEFAULT_CONFIG = {
    'TZ': 'America/New_York',
    'LOG_LEVEL': 'INFO',
    'DATA_DIR': '/data',
    'BOOKS_DIR': '/books',
    'EXTRA_EBOOK_DIRS': '',
    'ABS_COLLECTION_NAME': 'Synced with KOReader',
    'BOOKLORE_SHELF_NAME': 'Kobo',
    'SYNC_PERIOD_MINS': '5',
    'SYNC_DELTA_ABS_SECONDS': '60',
    'SYNC_DELTA_KOSYNC_PERCENT': '0.5',
    'SYNC_DELTA_BETWEEN_CLIENTS_PERCENT': '0.5',
    'SYNC_DELTA_KOSYNC_WORDS': '400',
    'SYNC_FRESHNESS_GUARDS': 'true',
    'SYNC_ROLLBACK_VETO_SECONDS': '600',
    # A normal locator round-trip snap is single-digit seconds; a segment seam
    # (out-of-order narration, issue #426) can be minutes to hours for a char
    # delta of only a couple of characters — 30s comfortably clears the former
    # while still catching the latter.
    'LOCATOR_ROUNDTRIP_TOLERANCE_SECONDS': '30',
    'KOREADER_SESSION_GAP_MINUTES': '30',
    'READING_SESSION_MERGE_MINUTES': '5',
    'FUZZY_MATCH_THRESHOLD': '80',
    'WHISPER_MODEL': 'tiny',
    'WHISPER_DEVICE': 'auto',
    'WHISPER_COMPUTE_TYPE': 'auto',
    'TRANSCRIPTION_PROVIDER': 'local',
    'WHISPER_CPP_URL': '',
    'WHISPER_CPP_TIMEOUT': '600',
    'WHISPER_CPP_SEND_ORIGINAL': 'false',
    'WHISPER_CPP_CHUNK_MINUTES': '0',
    # CTC forced alignment (opt-in; needs the CTC-enabled image with torch/torchaudio).
    'CTC_ENABLED': 'false',
    'CTC_MODEL': 'mms_fa',
    'CTC_DEVICE': 'auto',
    # Per-chapter RANSAC segment placement (issue #426 phase 2), replacing the global
    # monotonic LIS filter only for books whose narration order genuinely differs from
    # spine order. See docs/PLAN_OUT_OF_ORDER_NARRATION.md and
    # AlignmentService.segmented_maps_enabled().
    'ALIGNMENT_SEGMENTED_MAPS': 'false',
    # Non-LLM content-match guard (issue #426): n-gram overlap fallback used when the
    # embedding path (OLLAMA_ALIGN_CONTENT_GUARD) is unavailable. See
    # AlignmentService._verify_content_match / map_quality.transcript_text_overlap for
    # the calibration data behind the 0.25 default.
    'CONTENT_MATCH_GUARD': 'true',
    'CONTENT_MATCH_MIN_OVERLAP': '0.15',
    'AUDIO_SPLIT_DURATION_MINUTES': '45',
    'TRANSCRIPT_MIN_COVERAGE': '0.85',
    # Byte budget for a diagnostics upload. The receiver rejects larger bodies with
    # HTTP 413 and a 413 never clears the sender's snapshot, so an oversized payload
    # would resend and re-fail forever. 0 disables shedding.
    'DIAGNOSTICS_MAX_PAYLOAD_BYTES': '800000',
    'SHARE_ALL_BOOKS_WITH_ALL_USERS': 'false',
    'DEEPGRAM_API_KEY': '',
    'DEEPGRAM_MODEL': 'nova-2',
    'JOB_MAX_RETRIES': '5',
    'JOB_RETRY_DELAY_MINS': '15',
    'AUDIOBOOKS_DIR': '/audiobooks',
    'STORYTELLER_LIBRARY_DIR': '/storyteller_library',
    'STORYTELLER_ASSETS_DIR': '',
    'STORYTELLER_UPLOAD_CHUNK_SIZE': '5242880',
    'STORYTELLER_NO_EPUB_CACHE': 'false',
    'STORYTELLER_RECOVERY_MAX_WAIT_MINUTES': '360',
    'STORYTELLER_RECOVERY_POLL_INTERVAL_MINUTES': '2',
    'ABS_PROGRESS_OFFSET_SECONDS': '0',
    'EBOOK_CACHE_SIZE': '3',
    'ALIGNMENT_CACHE_SIZE': '3',
    'KOSYNC_HASH_METHOD': 'content',
    'KOSYNC_AUTH_METHOD': 'kosync',
    'KOSYNC_AUTO_MAP_ON_AGREEMENT': 'true',
    'KOREADER_COMBINE_DEVICE_STATS': 'true',
    'KOREADER_ANNOTATION_SYNC': 'true',
    'KOSYNC_PUT_DEBOUNCE_SECONDS': '300',
    'KOSYNC_RECENT_EXTERNAL_PUT_SECONDS': '600',
    'TELEGRAM_LOG_LEVEL': 'ERROR',
    'DIAGNOSTICS_OPT_IN': 'false',
    'DIAGNOSTICS_PROMPTED': '',
    'DIAGNOSTICS_INSTANCE_ID': '',
    'SERVICE_GATE_RECONCILED': '',
    'DIAGNOSTICS_ENDPOINT_URL': 'https://bookbridge-diagnostics.duckdns.org/api/v1/diagnostics',
    'DIAGNOSTICS_LAST_SENT': '',
    'DIAGNOSTICS_INGEST_TOKEN': '',
    'SHELFMARK_URL': '',
    'SHELFMARK_ENABLED': 'false',
    'KOSYNC_ENABLED': 'false',
    'KOSYNC_HASH_RECONCILE_ENABLED': 'true',
    'KOSYNC_XPATH_ORDER_ENABLED': 'false',
    'KOSYNC_FURTHEST_WINS': 'true',
    'KOSYNC_HASH_RECONCILE_MINUTES': '360',
    'KOSYNC_BOOKORBIT_DISCOVERY_LIMIT': '40',
    'STORYTELLER_ENABLED': 'false',
    'BOOKLORE_ENABLED': 'false',
    'BOOKLORE_LIBRARY_ID': '',
    'GRIMMORY_READING_SESSIONS': 'true',
    'BOOKLORE_ANNOTATION_SYNC': 'false',
    'BOOKLORE_ANNOTATION_SYNC_MINUTES': '15',
    'DEVICE_SYNC_COLLECTION_SOURCE': 'grimmory',
    'DEVICE_SYNC_COLLECTIONS': 'off',
    'DEVICE_SYNC_EXCLUDED_SHELVES': '',
    'DEVICE_SYNC_HARDCOVER_LISTS': 'all',
    'DEVICE_SYNC_HARDCOVER_LIST_NAMES': '',
    'DEVICE_SYNC_EBOOK_CACHE_TTL_MINUTES': '360',
    'BOOKLORE_SHELF_WATCH_ENABLED': 'false',
    'BOOKLORE_SHELF_WATCH_NAME': 'Up Next',
    'BOOKLORE_SHELF_WATCH_THRESHOLD': '95',
    'BOOKLORE_SHELF_WATCH_RESCAN_HOURS': '24',
    'BOOKORBIT_ENABLED': 'false',
    'BOOKORBIT_SERVER': '',
    'ABS_WEB_URL': '',
    # ABS defaults to enabled so existing installs keep working when the key is
    # reconciled in bootstrap_config(). Every other service defaults 'false'.
    'ABS_ENABLED': 'true',
    'BOOKLORE_WEB_URL': '',
    'BOOKORBIT_WEB_URL': '',
    'CWA_WEB_URL': '',
    'BOOKORBIT_USER': '',
    'BOOKORBIT_PASSWORD': '',
    'BOOKORBIT_SHELF_NAME': 'Kobo',
    'BOOKORBIT_POLL_MODE': 'global',
    'BOOKORBIT_POLL_SECONDS': '300',
    'BOOKORBIT_POLL_WAIT_FOR_SETTLE': 'false',
    'BOOKORBIT_AUDIO_POLL_MODE': 'global',
    'BOOKORBIT_AUDIO_POLL_SECONDS': '300',
    'BOOKORBIT_AUDIO_POLL_WAIT_FOR_SETTLE': 'false',
    'BOOKORBIT_READING_SESSIONS': 'true',
    'BOOKORBIT_SHELF_WATCH_ENABLED': 'false',
    'BOOKORBIT_SHELF_WATCH_NAME': 'Up Next',
    'BOOKORBIT_SHELF_WATCH_THRESHOLD': '95',
    'BOOKORBIT_SHELF_WATCH_RESCAN_HOURS': '24',
    'BOOKORBIT_ANNOTATION_SYNC_MINUTES': '15',
    'BOOKORBIT_KOSYNC_OWNER': '',
    'KAVITA_ENABLED': 'false',
    'KAVITA_SERVER': '',
    'KAVITA_WEB_URL': '',
    'KAVITA_API_KEY': '',
    'KAVITA_LIBRARY_ID': '',
    'KAVITA_COLLECTION_NAME': 'BookBridge',
    'KAVITA_POLL_MODE': 'global',
    'KAVITA_POLL_SECONDS': '300',
    'KAVITA_POLL_WAIT_FOR_SETTLE': 'false',
    'KAVITA_SHELF_WATCH_ENABLED': 'false',
    'KAVITA_SHELF_WATCH_NAME': 'Up Next',
    'KAVITA_SHELF_WATCH_THRESHOLD': '95',
    'KAVITA_SHELF_WATCH_RESCAN_HOURS': '24',
    'BOOKFUSION_ENABLED': 'false',
    'BOOKFUSION_API_URL': 'https://www.bookfusion.com',
    'BOOKFUSION_ACCESS_TOKEN': '',
    'BOOKFUSION_API_KEY': '',
    'BOOKFUSION_ANNOTATION_SYNC': 'false',
    'BOOKFUSION_POLL_MODE': 'global',
    'BOOKFUSION_POLL_SECONDS': '300',
    'BOOKFUSION_POLL_WAIT_FOR_SETTLE': 'false',
    'CWA_ENABLED': 'false',
    'CWA_SERVER': '',
    'CWA_USERNAME': '',
    'CWA_PASSWORD': '',
    'CWA_SYNC_ENABLED': 'false',
    'CWA_SYNC_TOKEN': '',
    'CWA_KOBO_SPAN_SYNC': 'true',
    'CWA_SYNC_POLL_MODE': 'global',
    'CWA_SYNC_POLL_SECONDS': '300',
    'CWA_SYNC_POLL_WAIT_FOR_SETTLE': 'false',
    'CALIBRE_USE_ABS_IDENTIFIER': 'false',
    'CALIBRE_LIBRARY_PATH': '',
    # Readest predates its own service gate, so it defaults on: the per-user
    # highlight/upload flags are what actually start any work.
    'READEST_ENABLED': 'true',
    'READEST_ANNOTATION_SYNC': 'false',
    'READEST_ANNOTATION_SYNC_MINUTES': '15',
    'READEST_EMAIL': '',
    'READEST_PASSWORD': '',
    'READEST_ACCESS_TOKEN': '',
    'READEST_REFRESH_TOKEN': '',
    'READEST_TOKEN_EXPIRES_AT': '',
    'READEST_SUPABASE_URL': 'https://readest.supabase.co',
    'READEST_SUPABASE_ANON_KEY': '',
    'READEST_UPLOAD_ON_MATCH': 'false',
    'READEST_GROUP_NAME': 'BookBridge',
    'READEST_UPLOAD_READING': 'false',
    'READEST_UPLOAD_MAX_PER_RUN': '5',
    'READEST_UPLOAD_SWEEP_MINUTES': '60',
    'HARDCOVER_ENABLED': 'false',
    'HARDCOVER_UPDATE_COOLDOWN_MINS': '60',
    'HARDCOVER_ANNOTATION_SYNC': 'false',
    'HARDCOVER_ANNOTATION_SYNC_MINUTES': '30',
    'HARDCOVER_GRIMMORY_LIST_SYNC': 'off',
    'HARDCOVER_GRIMMORY_LIST_PREFIX': 'Grimmory: ',
    'HARDCOVER_GRIMMORY_LIST_EXCLUDED_SHELVES': '',
    'STORYGRAPH_ENABLED': 'false',
    'STORYGRAPH_UPDATE_COOLDOWN_MINS': '60',
    'LLM_PROVIDER': 'ollama',
    'LLM_BASE_URL': 'http://localhost:8080/v1',
    'LLM_API_KEY': '',
    'LLM_EMBED_MODEL': '',
    'LLM_CHAT_MODEL': '',
    'LLM_NUM_CTX': '',
    'OLLAMA_ENABLED': 'false',
    'OLLAMA_URL': 'http://ollama:11434',
    'OLLAMA_EMBED_MODEL': 'nomic-embed-text',
    'OLLAMA_CHAT_MODEL': 'qwen2.5:14b',
    'OLLAMA_KEEP_ALIVE': '5m',
    'OLLAMA_NUM_CTX': '',
    'OLLAMA_RERANK_SUGGESTIONS': 'true',
    'OLLAMA_RERANK_BAND_MIN': '60',
    'OLLAMA_RERANK_BAND_MAX': '95',
    'OLLAMA_JUDGE_SUGGESTIONS': 'true',
    'OLLAMA_JUDGE_MARGIN': '5',
    'OLLAMA_JUDGE_CONFIDENCE_MIN': '85',
    'OLLAMA_ALIGN_FALLBACK': 'true',
    'OLLAMA_EBOOK_TEXT_FALLBACK': 'true',
    'OLLAMA_ALIGN_SIM_THRESHOLD': '0.72',
    'OLLAMA_ALIGN_ANCHOR_RESCUE': 'true',
    'OLLAMA_ALIGN_MAX_WINDOWS': '80',
    'OLLAMA_ALIGN_CONTENT_GUARD': 'true',
    'OLLAMA_ALIGN_CONTENT_MIN_SIM': '0.45',
    'OLLAMA_SUGGEST_JUDGE_GATE': 'true',
    'OLLAMA_SUGGEST_AUTOKEEP_SCORE': '90',
    'OLLAMA_TRACKER_MATCH': 'true',
    'OLLAMA_LIBRARY_MATCH': 'true',
    'TELEGRAM_ENABLED': 'false',
    'SUGGESTIONS_ENABLED': 'false',
    'SUGGESTIONS_AUTO_MATCH_ENABLED': 'false',
    'SUGGESTIONS_AUTO_MATCH_THRESHOLD': '100',
    'KOSYNC_USE_PERCENTAGE_FROM_SERVER': 'false',
    'SYNC_ABS_EBOOK': 'false',
    # cfi | readium | auto. Measured on real devices: a CFI is readable by every ABS
    # client (exact in the official app), while a Readium locator opens at the cover
    # in the official app and web reader. 'cfi' is therefore the safe default and
    # matches main's long-standing behaviour; 'auto' mirrors whatever shape the reader
    # already stored, which is more precise for Readium-only readers but strands
    # anyone who also opens the official app.
    'ABS_EBOOK_LOCATOR_FORMAT': 'cfi',
    'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT': 'true',
    'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT': 'false',
    'ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID': 'false',
    'ABS_SOCKET_ENABLED': 'true',
    'SYNC_COMPLETION_PROPAGATION': 'false',
    'SYNC_COMPLETION_THRESHOLD': '99',
    'REMOTE_AUTH_ENABLED': 'false',
    'REMOTE_AUTH_HEADER': 'Remote-User',
    # Empty means loopback only. Deliberately fail-closed: the header is a full
    # authentication bypass for anything that can reach the port, so a permissive
    # default (e.g. all RFC1918) would not protect the common threat — another
    # machine on the same LAN as the published port.
    'REMOTE_AUTH_TRUSTED_PROXIES': '',
    'ABS_SOCKET_DEBOUNCE_SECONDS': '30',
    'INSTANT_SYNC_ENABLED': 'true',
    'STORYTELLER_POLL_MODE': 'global',
    'STORYTELLER_POLL_SECONDS': '45',
    'STORYTELLER_POLL_WAIT_FOR_SETTLE': 'false',
    'STORYTELLER_LISTENING_SESSIONS': 'true',
    'BOOKLORE_POLL_MODE': 'global',
    'BOOKLORE_POLL_SECONDS': '300',
    'BOOKLORE_POLL_WAIT_FOR_SETTLE': 'false',
    'BOOKLORE_AUDIO_POLL_MODE': 'global',
    'BOOKLORE_AUDIO_POLL_SECONDS': '300',
    'BOOKLORE_AUDIO_POLL_WAIT_FOR_SETTLE': 'false',
    'SMIL_VALIDATION_THRESHOLD': '60',
}


# Every key the settings form is allowed to persist. The POSTed form also carries
# control fields — the csrf_token the CSRF bootstrap script injects into every
# form, and the Users-tab action/user_id/username/password/role — which are not
# settings and must never reach the settings table. Union of both registries so a
# key registered in only one of them still saves.
KNOWN_SETTING_KEYS = frozenset(ALL_SETTINGS) | frozenset(DEFAULT_CONFIG)

class ConfigLoader:
    """
    Loads configuration from database and updates environment variables.
    Settings in the database take precedence over environment variables,
    except for critical paths that might be needed to connect to the DB itself.
    """

    @staticmethod
    def bootstrap_config(db_service: DatabaseService):
        """
        If settings table is empty, populate it from os.environ or defaults.
        This provides a smooth migration for existing users.
        """
        try:
            # Check if we have any settings
            existing_settings = db_service.get_all_settings()
            if existing_settings:
                # Already bootstrapped: reconcile by adding any NEW settings keys that
                # were introduced after this install was first seeded. Additive only —
                # never overwrites a value the user has already set.
                missing = [k for k in ALL_SETTINGS if k not in existing_settings]
                for key in missing:
                    # Honor an env override for a newly-added key (same precedence as
                    # the fresh-bootstrap path), so a compose-configured value isn't
                    # silently replaced by the default and then loaded back as empty.
                    db_service.set_setting(key, str(os.environ.get(key, DEFAULT_CONFIG.get(key, ""))))
                if missing:
                    logger.info(f"➕ Added {len(missing)} new setting(s) to existing config: {missing}")
                return

            logger.info("🚀 Bootstrapping configuration from environment variables...")
            
            count = 0
            for key in ALL_SETTINGS:
                # Priority: 1. Env Var, 2. Default, 3. Empty string
                val = os.environ.get(key, DEFAULT_CONFIG.get(key, ""))
                
                # Check for None explicitly
                if val is None:
                    val = ""
                
                db_service.set_setting(key, str(val))
                count += 1
            
            logger.info(f"✅ Bootstrapped {count} settings to database")

        except Exception as e:
            logger.error(f"❌ Error bootstrapping config: {e}", exc_info=True)

    @staticmethod
    def load_settings(db_service: DatabaseService):
        """
        Load all settings from database and update os.environ.
        
        Args:
            db_service: Initialized DatabaseService instance
        """
        try:
            settings = db_service.get_all_settings()
            count = 0
            
            for key, value in settings.items():
                # Apply validation or type conversion if needed (mostly string for env vars)
                val_str = str(value) if value is not None else ""
                
                # DB values always win — if the user cleared a field, honor it
                os.environ[key] = val_str
                
                # Mask secrets in logs
                log_val = "******" if any(s in key for s in ['KEY', 'PASSWORD', 'TOKEN']) else val_str
                # logger.debug(f"Loaded {key}={log_val}")
                count += 1
            
            logger.info(f"⚙️  Loaded {count} settings from database")
            
        except Exception as e:
            logger.error(f"❌ Error loading settings from database: {e}", exc_info=True)
            # Do not re-raise, fall back to existing env vars
