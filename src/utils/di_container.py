#!/usr/bin/env python3
"""
Dependency Injection Container for abs-kosync-bridge.
Using python-dependency-injector library for proper DI functionality.
"""

import logging
from pathlib import Path
import os

from dependency_injector import containers, providers

# Import all the classes we'll be using
from src.api.api_clients import ABSClient, KoSyncClient
from src.api.booklore_client import BookloreClient
from src.api.bookorbit_client import BookOrbitClient
from src.api.cwa_client import CWAClient
from src.api.cwa_sync_api import CWASyncApi
from src.api.hardcover_client import HardcoverClient
from src.api.ollama_client import OllamaClient
from src.api.storygraph_client import StorygraphClient
from src.api.storyteller_api import StorytellerAPIClient
from src.db.database_service import DatabaseService
from src.utils.ebook_utils import EbookParser
from src.utils.transcriber import AudioTranscriber
from src.utils.smil_extractor import SmilExtractor
from src.utils.polisher import Polisher # [NEW]
from src.services.alignment_service import AlignmentService # [NEW]
from src.services.library_service import LibraryService # [NEW]
from src.services.migration_service import MigrationService # [NEW]
from src.services.forge_service import ForgeService
from src.services.koreader_device_sync_service import KOReaderDeviceSyncService
from src.services.audio_source_adapters import ABSAudioSourceAdapter, BookLoreAudioSourceAdapter
from src.services.calibre_identifier_resolver import CalibreIdentifierResolver
from src.services.book_mapping_service import BookMappingService
from src.services.shelf_watch_service import ShelfWatchService
from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.booklore_audio_sync_client import BookLoreAudioSyncClient
from src.sync_clients.bookorbit_sync_client import BookOrbitSyncClient
from src.sync_clients.bookorbit_audio_sync_client import BookOrbitAudioSyncClient
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.sync_clients.cwa_sync_client import CWASyncClient
from src.sync_clients.hardcover_sync_client import HardcoverSyncClient
from src.sync_clients.storygraph_sync_client import StorygraphSyncClient
from src.sync_manager import SyncManager

logger = logging.getLogger(__name__)

class Container(containers.DeclarativeContainer):
    """Main dependency injection container using dependency-injector library."""

    # Configuration
    config = providers.Configuration()

    # Configuration values from environment (Lazy evaluation)
    data_dir = providers.Factory(
        lambda: Path(os.environ.get("DATA_DIR", "/data"))
    )
    
    books_dir = providers.Factory(
        lambda: Path(os.environ.get("BOOKS_DIR", "/books"))
    )
    
    db_file = providers.Factory(
        lambda data_dir: data_dir / "mapping_db.json",
        data_dir=data_dir
    )
    state_file = providers.Factory(
        lambda data_dir: data_dir / "last_state.json",
        data_dir=data_dir
    )
    epub_cache_dir = providers.Factory(
        lambda data_dir: data_dir / "epub_cache",
        data_dir=data_dir
    )
    
    # Lazy load specific config values
    delta_abs_thresh = providers.Factory(lambda: float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60)))
    delta_kosync_thresh = providers.Factory(lambda: float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0)
    kosync_use_percentage_from_server = providers.Factory(lambda: os.getenv("KOSYNC_USE_PERCENTAGE_FROM_SERVER", "false").lower() == "true")

    # API Clients
    abs_client = providers.Singleton(ABSClient)

    kosync_client = providers.Singleton(KoSyncClient)

    # SQLAlchemy Database Service - Moved up for dependency injection
    database_service = providers.Singleton(
        DatabaseService,
        providers.Factory(
            lambda data_dir: str(data_dir / "database.db"),
            data_dir=data_dir
        )
    )

    ollama_client = providers.Singleton(OllamaClient)

    booklore_client = providers.Singleton(
        BookloreClient,
        database_service=database_service,
        ollama_client=ollama_client
    )
    bookorbit_client = providers.Singleton(BookOrbitClient, ollama_client=ollama_client)
    kavita_client = providers.Object(None)

    hardcover_client = providers.Singleton(HardcoverClient)
    storygraph_client = providers.Singleton(StorygraphClient)

    cwa_client = providers.Singleton(CWAClient)

    cwa_sync_api = providers.Singleton(
        CWASyncApi,
        cwa_client=cwa_client
    )

    calibre_identifier_resolver = providers.Singleton(
        CalibreIdentifierResolver,
        cwa_client=cwa_client,
    )

    # Ebook parser
    ebook_parser = providers.Singleton(
        EbookParser,
        books_dir,
        epub_cache_dir=epub_cache_dir,
        ollama_client=ollama_client
    )

    # Smil Extractor Provider
    smil_extractor = providers.Singleton(
        SmilExtractor
    )

    # [NEW] Polisher
    polisher = providers.Singleton(
        Polisher
    )

    # [NEW] Services
    alignment_service = providers.Singleton(
        AlignmentService,
        database_service=database_service,
        polisher=polisher,
        ollama_client=ollama_client
    )

    library_service = providers.Singleton(
        LibraryService,
        database_service=database_service,
        booklore_client=booklore_client,
        cwa_client=cwa_client,
        abs_client=abs_client,
        epub_cache_dir=epub_cache_dir
    )

    koreader_device_sync_service = providers.Singleton(
        KOReaderDeviceSyncService,
        database_service=database_service,
        ebook_parser=ebook_parser,
        abs_client=abs_client,
        booklore_client=booklore_client,
        cwa_client=cwa_client,
        kavita_client=kavita_client,
        epub_cache_dir=epub_cache_dir,
    )

    migration_service = providers.Singleton(
        MigrationService,
        database_service=database_service,
        alignment_service=alignment_service,
        data_dir=data_dir
    )

    # Storyteller client with factory
    storyteller_client = providers.Singleton(
        StorytellerAPIClient
    )

    # Transcriber
    transcriber = providers.Singleton(
        AudioTranscriber,
        data_dir,
        smil_extractor,
        polisher,  # [UPDATED] Injected dependency
        ollama_client=ollama_client
    )

    # Sync clients
    abs_sync_client = providers.Singleton(
        ABSSyncClient,
        abs_client,
        transcriber,
        ebook_parser,
        alignment_service
    )

    kosync_sync_client = providers.Singleton(
        KoSyncSyncClient,
        kosync_client,
        ebook_parser
    )

    storyteller_sync_client = providers.Singleton(
        StorytellerSyncClient,
        storyteller_client,
        ebook_parser,
        database_service
    )

    booklore_sync_client = providers.Singleton(
        BookloreSyncClient,
        booklore_client,
        ebook_parser
    )

    booklore_audio_sync_client = providers.Singleton(
        BookLoreAudioSyncClient,
        booklore_client,
        ebook_parser,
        alignment_service=alignment_service,
    )

    bookorbit_sync_client = providers.Singleton(
        BookOrbitSyncClient,
        bookorbit_client,
        ebook_parser,
    )

    bookorbit_audio_sync_client = providers.Singleton(
        BookOrbitAudioSyncClient,
        bookorbit_client,
        ebook_parser,
        alignment_service=alignment_service,
    )

    abs_ebook_sync_client = providers.Singleton(
        ABSEbookSyncClient,
        abs_client,
        ebook_parser
    )

    cwa_sync_client = providers.Singleton(
        CWASyncClient,
        cwa_sync_api,
        cwa_client,
        ebook_parser
    )

    hardcover_sync_client = providers.Singleton(
        HardcoverSyncClient,
        hardcover_client,
        ebook_parser,
        abs_client,
        database_service,
        ollama_client=ollama_client
    )

    storygraph_sync_client = providers.Singleton(
        StorygraphSyncClient,
        storygraph_client,
        ebook_parser,
        abs_client,
        database_service,
        ollama_client=ollama_client
    )

    abs_audio_source_adapter = providers.Singleton(
        ABSAudioSourceAdapter,
        abs_client=abs_client,
    )

    booklore_audio_source_adapter = providers.Singleton(
        BookLoreAudioSourceAdapter,
        booklore_client=booklore_client,
        data_dir=data_dir,
    )

    audio_source_adapters = providers.Dict(
        ABS=abs_audio_source_adapter,
        BookLore=booklore_audio_source_adapter,
    )

    # Sync clients dictionary for reuse
    sync_clients = providers.Dict(
        ABS=abs_sync_client,
        ABSEbook=abs_ebook_sync_client,
        KoSync=kosync_sync_client,
        Storyteller=storyteller_sync_client,
        BookLore=booklore_sync_client,
        BookLoreAudio=booklore_audio_sync_client,
        BookOrbit=bookorbit_sync_client,
        BookOrbitAudio=bookorbit_audio_sync_client,
        CWA=cwa_sync_client,
        Hardcover=hardcover_sync_client,
        StoryGraph=storygraph_sync_client
    )

    # Constructed after the sync clients so forge completion can run the same
    # Hardcover/StoryGraph automatch as the regular match path.
    forge_service = providers.Singleton(
        ForgeService,
        database_service=database_service,
        abs_client=abs_client,
        booklore_client=booklore_client,
        bookorbit_client=bookorbit_client,
        storyteller_client=storyteller_client,
        library_service=library_service,
        ebook_parser=ebook_parser,
        transcriber=transcriber,
        alignment_service=alignment_service,
        sync_clients=providers.Dict(
            Hardcover=hardcover_sync_client,
            StoryGraph=storygraph_sync_client,
        ),
    )

    # Book mapping helper for shelf-watch auto-matches + ebook-only fallbacks.
    # Constructed late so it can pull sync_clients (also a Singleton) for Hardcover/StoryGraph automatch.
    book_mapping_service = providers.Singleton(
        BookMappingService,
        database_service=database_service,
        booklore_client=booklore_client,
        bookorbit_client=bookorbit_client,
        ebook_parser=ebook_parser,
        abs_client=abs_client,
        sync_clients=providers.Dict(
            Hardcover=hardcover_sync_client,
            StoryGraph=storygraph_sync_client,
        ),
    )

    # "Up Next" shelf watchers (Grimmory + BookOrbit). SuggestionsService is
    # lazy-imported from web_server at first use to avoid the module-load-time
    # cycle (web_server owns the closures the SuggestionsService captures).
    shelf_watch_service = providers.Singleton(
        ShelfWatchService,
        booklore_client=booklore_client,
        database_service=database_service,
        book_mapping_service=book_mapping_service,
        source_name='BookLore',
        env_prefix='BOOKLORE',
    )

    shelf_watch_service_bookorbit = providers.Singleton(
        ShelfWatchService,
        booklore_client=bookorbit_client,
        database_service=database_service,
        book_mapping_service=book_mapping_service,
        source_name='BookOrbit',
        env_prefix='BOOKORBIT',
    )

    shelf_watch_services = providers.List(
        shelf_watch_service,
        shelf_watch_service_bookorbit,
    )

    shelf_watch_services_by_client = providers.Dict(
        BookLore=shelf_watch_service,
        BookOrbit=shelf_watch_service_bookorbit,
    )

    # Sync Manager
    sync_manager = providers.Singleton(
        SyncManager,
        abs_client=abs_client,
        booklore_client=booklore_client,
        bookorbit_client=bookorbit_client,
        hardcover_client=hardcover_client,
        storyteller_client=storyteller_client,
        transcriber=transcriber,
        ebook_parser=ebook_parser,
        database_service=database_service,
        sync_clients=sync_clients,
        
        # [NEW] Injected Services
        alignment_service=alignment_service,
        library_service=library_service,
        migration_service=migration_service,
        shelf_watch_service=shelf_watch_service,
        shelf_watch_services=shelf_watch_services,
        audio_source_adapters=audio_source_adapters,

        epub_cache_dir=epub_cache_dir,
        data_dir=data_dir,
        books_dir=books_dir
    )


# Global container instance
container = Container()

def create_container() -> Container:
    """Create and configure the DI container with all application dependencies."""
    return container
