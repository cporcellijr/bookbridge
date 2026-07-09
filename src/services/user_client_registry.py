"""Per-user client registry (multi-user Phase 2).

Builds and caches a per-user bundle of API + sync clients, each constructed
with that user's stored credentials (overlaid on the global config). Catalog
services — database, ebook parser, alignment, transcriber, ollama — are SHARED
(passed in) because the library/mappings/alignments are shared across users.

A bundle mirrors the client wiring in di_container, but scoped to one user.
"""

import logging
import threading
from dataclasses import dataclass, field

from src.utils.user_config import PER_USER_CREDENTIAL_KEYS, _ALLOW_GLOBAL_FALLBACK_KEY

from src.api.api_clients import ABSClient, KoSyncClient
from src.api.storyteller_api import StorytellerAPIClient
from src.api.cwa_client import CWAClient
from src.api.cwa_sync_api import CWASyncApi
from src.api.bookorbit_client import BookOrbitClient
from src.api.bookfusion_client import BookFusionClient
from src.api.booklore_client import BookloreClient
from src.api.hardcover_client import HardcoverClient
from src.api.storygraph_client import StorygraphClient

from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.bookfusion_sync_client import BookFusionSyncClient
from src.sync_clients.booklore_audio_sync_client import BookLoreAudioSyncClient
from src.sync_clients.bookorbit_sync_client import BookOrbitSyncClient
from src.sync_clients.bookorbit_audio_sync_client import BookOrbitAudioSyncClient
from src.sync_clients.cwa_sync_client import CWASyncClient
from src.sync_clients.hardcover_sync_client import HardcoverSyncClient
from src.sync_clients.storygraph_sync_client import StorygraphSyncClient
from src.services.library_service import LibraryService

logger = logging.getLogger(__name__)


@dataclass
class UserClients:
    """A user's API clients + sync-client dict (catalog services are shared)."""
    user_id: int
    abs_client: object
    kosync_client: object
    storyteller_client: object
    cwa_client: object
    bookorbit_client: object
    bookfusion_client: object
    booklore_client: object
    hardcover_client: object
    storygraph_client: object
    library_service: object = None
    sync_clients: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)


class UserClientRegistry:
    def __init__(self, database_service, ebook_parser, alignment_service,
                 transcriber=None, ollama_client=None, epub_cache_dir=None):
        self.database_service = database_service
        self.ebook_parser = ebook_parser
        self.alignment_service = alignment_service
        self.transcriber = transcriber
        self.ollama_client = ollama_client
        self.epub_cache_dir = epub_cache_dir
        self._cache = {}
        self._lock = threading.RLock()

    def _user_credentials(self, user_id: int) -> dict:
        """Per-user override dict, limited to recognized per-user keys."""
        try:
            stored = self.database_service.get_user_credentials(user_id) or {}
        except Exception as e:
            logger.warning("Could not load credentials for user %s: %s", user_id, e)
            stored = {}
        creds = {k: v for k, v in stored.items() if k in PER_USER_CREDENTIAL_KEYS}
        try:
            user = self.database_service.get_user(user_id)
            creds[_ALLOW_GLOBAL_FALLBACK_KEY] = bool(user and getattr(user, "is_admin", False))
        except Exception as e:
            logger.warning("Could not load user %s for credential fallback policy: %s", user_id, e)
            creds[_ALLOW_GLOBAL_FALLBACK_KEY] = False
        return creds

    def get_clients(self, user_id: int) -> UserClients:
        """Return the cached per-user bundle, building it on first access."""
        with self._lock:
            bundle = self._cache.get(user_id)
            if bundle is None:
                bundle = self._build(user_id, self._user_credentials(user_id))
                self._cache[user_id] = bundle
            return bundle

    def invalidate(self, user_id: int = None) -> None:
        """Drop cached bundle(s) — call after a user's credentials change."""
        with self._lock:
            if user_id is None:
                self._cache.clear()
            else:
                self._cache.pop(user_id, None)

    def _build(self, user_id: int, creds: dict) -> UserClients:
        ep = self.ebook_parser
        align = self.alignment_service
        db = self.database_service

        abs_client = ABSClient(credentials=creds)
        kosync_client = KoSyncClient(credentials=creds)
        storyteller_client = StorytellerAPIClient(credentials=creds)
        cwa_client = CWAClient(credentials=creds)
        cwa_sync_api = CWASyncApi(cwa_client=cwa_client, credentials=creds)
        bookorbit_client = BookOrbitClient(ollama_client=self.ollama_client, credentials=creds)
        bookfusion_client = BookFusionClient(credentials=creds, database_service=db, user_id=user_id)
        booklore_client = BookloreClient(database_service=db, ollama_client=self.ollama_client, credentials=creds)
        hardcover_client = HardcoverClient(credentials=creds)
        storygraph_client = StorygraphClient(credentials=creds)

        # Per-user library service for ebook acquisition/search (uses this
        # user's abs/cwa/booklore clients). Requires an epub cache dir.
        library_service = None
        if self.epub_cache_dir is not None:
            library_service = LibraryService(
                database_service=db,
                booklore_client=booklore_client,
                cwa_client=cwa_client,
                abs_client=abs_client,
                epub_cache_dir=self.epub_cache_dir,
            )

        sync_clients = {
            "ABS": ABSSyncClient(abs_client, self.transcriber, ep, align),
            "ABSEbook": ABSEbookSyncClient(abs_client, ep),
            "KoSync": KoSyncSyncClient(kosync_client, ep),
            "Storyteller": StorytellerSyncClient(storyteller_client, ep, db),
            "BookLore": BookloreSyncClient(booklore_client, ep),
            "BookFusion": BookFusionSyncClient(bookfusion_client, ep, database_service=db, user_id=user_id),
            "BookLoreAudio": BookLoreAudioSyncClient(booklore_client, ep, alignment_service=align),
            "BookOrbit": BookOrbitSyncClient(bookorbit_client, ep),
            "BookOrbitAudio": BookOrbitAudioSyncClient(bookorbit_client, ep, alignment_service=align),
            "CWA": CWASyncClient(cwa_sync_api, cwa_client, ep),
            "Hardcover": HardcoverSyncClient(hardcover_client, ep, abs_client, db, ollama_client=self.ollama_client, booklore_client=booklore_client, bookorbit_client=bookorbit_client),
            "StoryGraph": StorygraphSyncClient(storygraph_client, ep, abs_client, db, ollama_client=self.ollama_client, booklore_client=booklore_client, bookorbit_client=bookorbit_client),
        }

        return UserClients(
            user_id=user_id,
            abs_client=abs_client,
            kosync_client=kosync_client,
            storyteller_client=storyteller_client,
            cwa_client=cwa_client,
            bookorbit_client=bookorbit_client,
            bookfusion_client=bookfusion_client,
            booklore_client=booklore_client,
            hardcover_client=hardcover_client,
            storygraph_client=storygraph_client,
            library_service=library_service,
            sync_clients=sync_clients,
            credentials=creds,
        )
