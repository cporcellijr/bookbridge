
import os
import logging
from src.db.database_service import DatabaseService

logger = logging.getLogger(__name__)

# Full list of settings to manage
ALL_SETTINGS = [
    # Required ABS
    'ABS_SERVER', 'ABS_KEY', 'ABS_LIBRARY_ID',
    
    # Optional ABS
    'ABS_COLLECTION_NAME', 'ABS_PROGRESS_OFFSET_SECONDS', 'ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID',
    'ABS_SOCKET_ENABLED', 'ABS_SOCKET_DEBOUNCE_SECONDS',
    
    # KOSync
    'KOSYNC_ENABLED', 'KOSYNC_SERVER', 'KOSYNC_USER', 'KOSYNC_KEY',
    'KOSYNC_HASH_METHOD', 'KOSYNC_USE_PERCENTAGE_FROM_SERVER',
    'KOSYNC_RECENT_EXTERNAL_PUT_SECONDS',
    
    # Storyteller
    'STORYTELLER_ENABLED', 'STORYTELLER_API_URL', 'STORYTELLER_USER', 'STORYTELLER_PASSWORD',
    
    # Grimmory
    'BOOKLORE_ENABLED', 'BOOKLORE_SERVER', 'BOOKLORE_USER', 'BOOKLORE_PASSWORD', 'BOOKLORE_SHELF_NAME', 'BOOKLORE_LIBRARY_ID',
    'GRIMMORY_READING_SESSIONS',
    'DEVICE_SYNC_COLLECTIONS', 'DEVICE_SYNC_EXCLUDED_SHELVES',
    'BOOKLORE_SHELF_WATCH_ENABLED', 'BOOKLORE_SHELF_WATCH_NAME',
    'BOOKLORE_SHELF_WATCH_THRESHOLD', 'BOOKLORE_SHELF_WATCH_RESCAN_HOURS',

    # BookOrbit
    'BOOKORBIT_ENABLED', 'BOOKORBIT_SERVER', 'BOOKORBIT_USER', 'BOOKORBIT_PASSWORD',
    'BOOKORBIT_SHELF_NAME', 'BOOKORBIT_POLL_MODE', 'BOOKORBIT_POLL_SECONDS',
    'BOOKORBIT_READING_SESSIONS',
    'BOOKORBIT_SHELF_WATCH_ENABLED', 'BOOKORBIT_SHELF_WATCH_NAME',
    'BOOKORBIT_SHELF_WATCH_THRESHOLD', 'BOOKORBIT_SHELF_WATCH_RESCAN_HOURS',

    # CWA (Calibre-Web Automated)
    'CWA_ENABLED', 'CWA_SERVER', 'CWA_USERNAME', 'CWA_PASSWORD',
    'CWA_SYNC_ENABLED', 'CWA_SYNC_TOKEN',
    'CWA_SYNC_POLL_MODE', 'CWA_SYNC_POLL_SECONDS',
    'CALIBRE_USE_ABS_IDENTIFIER', 'CALIBRE_LIBRARY_PATH',

    # Progress Tracker
    'PROGRESS_TRACKER_PROVIDER',

    # Hardcover
    'HARDCOVER_ENABLED', 'HARDCOVER_TOKEN',
    
    # StoryGraph
    'STORYGRAPH_ENABLED', 'STORYGRAPH_SESSION_COOKIE', 'STORYGRAPH_REMEMBER_USER_TOKEN',
    
    # Ollama (local LLM)
    'OLLAMA_ENABLED', 'OLLAMA_URL', 'OLLAMA_EMBED_MODEL', 'OLLAMA_CHAT_MODEL',
    'OLLAMA_RERANK_SUGGESTIONS', 'OLLAMA_RERANK_BAND_MIN', 'OLLAMA_RERANK_BAND_MAX',
    'OLLAMA_JUDGE_SUGGESTIONS', 'OLLAMA_JUDGE_MARGIN', 'OLLAMA_JUDGE_CONFIDENCE_MIN',
    'OLLAMA_ALIGN_FALLBACK', 'OLLAMA_ALIGN_SIM_THRESHOLD',
    'OLLAMA_TRACKER_MATCH',

    # Telegram
    'TELEGRAM_ENABLED', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'TELEGRAM_LOG_LEVEL',
    
    # Shelfmark
    'SHELFMARK_URL', 'SHELFMARK_ENABLED',
    
    # Sync Behavior
    'SYNC_PERIOD_MINS', 'SYNC_DELTA_ABS_SECONDS', 'SYNC_DELTA_KOSYNC_PERCENT',
    'SYNC_DELTA_BETWEEN_CLIENTS_PERCENT', 'SYNC_DELTA_KOSYNC_WORDS',
    'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT', 'SYNC_ABS_EBOOK', 'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT',
    'FUZZY_MATCH_THRESHOLD', 'SUGGESTIONS_ENABLED',
    'INSTANT_SYNC_ENABLED',
    'STORYTELLER_POLL_MODE', 'STORYTELLER_POLL_SECONDS',
    'BOOKLORE_POLL_MODE', 'BOOKLORE_POLL_SECONDS',
    
    # System
    'TZ', 'LOG_LEVEL', 'DATA_DIR', 'BOOKS_DIR', 
    'AUDIOBOOKS_DIR', 'STORYTELLER_LIBRARY_DIR', 'STORYTELLER_ASSETS_DIR', 'STORYTELLER_UPLOAD_CHUNK_SIZE',
    'STORYTELLER_NO_EPUB_CACHE',
    'STORYTELLER_RECOVERY_MAX_WAIT_MINUTES', 'STORYTELLER_RECOVERY_POLL_INTERVAL_MINUTES',
    'EBOOK_CACHE_SIZE',
    'JOB_MAX_RETRIES', 'JOB_RETRY_DELAY_MINS', 'WHISPER_MODEL',
    'WHISPER_DEVICE', 'WHISPER_COMPUTE_TYPE',
    'TRANSCRIPTION_PROVIDER', 'DEEPGRAM_API_KEY', 'DEEPGRAM_MODEL', 'WHISPER_CPP_URL',
    'SMIL_VALIDATION_THRESHOLD',
]

# Default values
DEFAULT_CONFIG = {
    'TZ': 'America/New_York',
    'LOG_LEVEL': 'INFO',
    'DATA_DIR': '/data',
    'BOOKS_DIR': '/books',
    'ABS_COLLECTION_NAME': 'Synced with KOReader',
    'BOOKLORE_SHELF_NAME': 'Kobo',
    'SYNC_PERIOD_MINS': '5',
    'SYNC_DELTA_ABS_SECONDS': '60',
    'SYNC_DELTA_KOSYNC_PERCENT': '0.5',
    'SYNC_DELTA_BETWEEN_CLIENTS_PERCENT': '0.5',
    'SYNC_DELTA_KOSYNC_WORDS': '400',
    'FUZZY_MATCH_THRESHOLD': '80',
    'WHISPER_MODEL': 'tiny',
    'WHISPER_DEVICE': 'auto',
    'WHISPER_COMPUTE_TYPE': 'auto',
    'TRANSCRIPTION_PROVIDER': 'local',
    'WHISPER_CPP_URL': '',
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
    'KOSYNC_HASH_METHOD': 'content',
    'KOSYNC_PUT_DEBOUNCE_SECONDS': '300',
    'KOSYNC_RECENT_EXTERNAL_PUT_SECONDS': '600',
    'TELEGRAM_LOG_LEVEL': 'ERROR',
    'SHELFMARK_URL': '',
    'SHELFMARK_ENABLED': 'false',
    'KOSYNC_ENABLED': 'false',
    'STORYTELLER_ENABLED': 'false',
    'BOOKLORE_ENABLED': 'false',
    'BOOKLORE_LIBRARY_ID': '',
    'GRIMMORY_READING_SESSIONS': 'true',
    'DEVICE_SYNC_COLLECTIONS': 'off',
    'DEVICE_SYNC_EXCLUDED_SHELVES': '',
    'BOOKLORE_SHELF_WATCH_ENABLED': 'false',
    'BOOKLORE_SHELF_WATCH_NAME': 'Up Next',
    'BOOKLORE_SHELF_WATCH_THRESHOLD': '95',
    'BOOKLORE_SHELF_WATCH_RESCAN_HOURS': '24',
    'BOOKORBIT_ENABLED': 'false',
    'BOOKORBIT_SERVER': '',
    'BOOKORBIT_USER': '',
    'BOOKORBIT_PASSWORD': '',
    'BOOKORBIT_SHELF_NAME': 'Kobo',
    'BOOKORBIT_POLL_MODE': 'global',
    'BOOKORBIT_POLL_SECONDS': '300',
    'BOOKORBIT_READING_SESSIONS': 'true',
    'BOOKORBIT_SHELF_WATCH_ENABLED': 'false',
    'BOOKORBIT_SHELF_WATCH_NAME': 'Up Next',
    'BOOKORBIT_SHELF_WATCH_THRESHOLD': '95',
    'BOOKORBIT_SHELF_WATCH_RESCAN_HOURS': '24',
    'CWA_ENABLED': 'false',
    'CWA_SERVER': '',
    'CWA_USERNAME': '',
    'CWA_PASSWORD': '',
    'CWA_SYNC_ENABLED': 'false',
    'CWA_SYNC_TOKEN': '',
    'CWA_SYNC_POLL_MODE': 'global',
    'CWA_SYNC_POLL_SECONDS': '300',
    'CALIBRE_USE_ABS_IDENTIFIER': 'false',
    'CALIBRE_LIBRARY_PATH': '',
    'PROGRESS_TRACKER_PROVIDER': 'none',
    'HARDCOVER_ENABLED': 'false',
    'STORYGRAPH_ENABLED': 'false',
    'OLLAMA_ENABLED': 'false',
    'OLLAMA_URL': 'http://ollama:11434',
    'OLLAMA_EMBED_MODEL': 'nomic-embed-text',
    'OLLAMA_CHAT_MODEL': 'qwen2.5:14b',
    'OLLAMA_RERANK_SUGGESTIONS': 'false',
    'OLLAMA_RERANK_BAND_MIN': '60',
    'OLLAMA_RERANK_BAND_MAX': '95',
    'OLLAMA_JUDGE_SUGGESTIONS': 'false',
    'OLLAMA_JUDGE_MARGIN': '5',
    'OLLAMA_JUDGE_CONFIDENCE_MIN': '85',
    'OLLAMA_ALIGN_FALLBACK': 'false',
    'OLLAMA_ALIGN_SIM_THRESHOLD': '0.72',
    'OLLAMA_TRACKER_MATCH': 'false',
    'TELEGRAM_ENABLED': 'false',
    'SUGGESTIONS_ENABLED': 'false',
    'KOSYNC_USE_PERCENTAGE_FROM_SERVER': 'false',
    'SYNC_ABS_EBOOK': 'false',
    'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT': 'true',
    'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT': 'false',
    'ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID': 'false',
    'ABS_SOCKET_ENABLED': 'true',
    'ABS_SOCKET_DEBOUNCE_SECONDS': '30',
    'INSTANT_SYNC_ENABLED': 'true',
    'STORYTELLER_POLL_MODE': 'global',
    'STORYTELLER_POLL_SECONDS': '45',
    'BOOKLORE_POLL_MODE': 'global',
    'BOOKLORE_POLL_SECONDS': '300',
    'SMIL_VALIDATION_THRESHOLD': '60',
}

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
                # Already bootstrapped
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
            logger.error(f"❌ Error bootstrapping config: {e}")

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
            logger.error(f"❌ Error loading settings from database: {e}")
            # Do not re-raise, fall back to existing env vars
