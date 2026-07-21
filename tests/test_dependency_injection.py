#!/usr/bin/env python3
"""
Test script to verify dependency injection is working properly.
"""

import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def test_dependency_injection():
    """Test that our DI container can create the SyncManager properly."""

    # Set up environment for testing
    os.environ['DATA_DIR'] = str(Path.cwd() / 'test_data')
    os.environ['BOOKS_DIR'] = str(Path.cwd() / 'test_books')
    os.environ['ABS_SERVER'] = 'http://localhost:13378'
    os.environ['ABS_TOKEN'] = 'test-token'

    # Create test directories
    Path('test_data').mkdir(exist_ok=True)
    Path('test_books').mkdir(exist_ok=True)

    try:
        print("[TEST] Testing Dependency Injection")
        print("=" * 50)

        # Test 1: Create DI container
        print("[INIT] Creating DI container...")
        from src.utils.di_container import create_container
        container = create_container()
        print("[OK] DI container created successfully")

        # Test 2: Test individual component creation
        print("\n[TEST] Testing individual components...")

        from src.api.api_clients import ABSClient, KoSyncClient
        from src.api.booklore_client import BookloreClient
        from src.api.hardcover_client import HardcoverClient
        from src.utils.ebook_utils import EbookParser

        abs_client = container.abs_client()
        print(f"[OK] ABSClient: {type(abs_client).__name__}")

        kosync_client = container.kosync_client()
        print(f"[OK] KoSyncClient: {type(kosync_client).__name__}")

        booklore_client = container.booklore_client()
        print(f"[OK] BookloreClient: {type(booklore_client).__name__}")

        ebook_parser = container.ebook_parser()
        print(f"[OK] EbookParser: {type(ebook_parser).__name__}")

        # Test 3: Test factory-created components
        print("\n[TEST] Testing factory components...")

        storyteller_db = container.storyteller_client()
        print(f"[OK] Storyteller DB: {type(storyteller_db).__name__}")

        db = container.database_service()
        print(f"[OK] DB: {type(db).__name__}")

        # Test 4: Test sync clients
        print("\n[TEST] Testing sync clients...")

        from src.sync_clients.abs_sync_client import ABSSyncClient
        from src.sync_clients.kosync_sync_client import KoSyncSyncClient
        from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
        from src.sync_clients.booklore_sync_client import BookloreSyncClient

        abs_sync_client = container.abs_sync_client()
        print(f"[OK] ABSSyncClient: {type(abs_sync_client).__name__}")

        kosync_sync_client = container.kosync_sync_client()
        print(f"[OK] KoSyncSyncClient: {type(kosync_sync_client).__name__}")

        storyteller_sync_client = container.storyteller_sync_client()
        print(f"[OK] StorytellerSyncClient: {type(storyteller_sync_client).__name__}")

        booklore_sync_client = container.booklore_sync_client()
        print(f"[OK] BookloreSyncClient: {type(booklore_sync_client).__name__}")

        cwa_sync_client = container.cwa_sync_client()
        print(f"[OK] CWASyncClient: {type(cwa_sync_client).__name__}")

        # Test 5: Test SyncManager creation with DI
        print("\n[TEST] Testing SyncManager creation with DI...")

        from src.sync_manager import SyncManager
        sync_manager = container.sync_manager()
        print(f"[OK] SyncManager created: {type(sync_manager).__name__}")

        # Test 6: Verify autowired dependencies
        print("\n[VERIFY] Verifying autowired dependencies...")

        # Check that SyncManager has all the right clients
        assert hasattr(sync_manager, 'abs_client'), "SyncManager missing abs_client"
        assert hasattr(sync_manager, 'booklore_client'), "SyncManager missing booklore_client"
        assert hasattr(sync_manager, 'ebook_parser'), "SyncManager missing ebook_parser"
        assert hasattr(sync_manager, 'sync_clients'), "SyncManager missing sync_clients"

        print("[OK] All dependencies properly autowired")

        # Test 7: Verify sync clients are configured properly
        print("\n[TEST] Testing sync client configurations...")

        configured_clients = [name for name, client in sync_manager.sync_clients.items()]
        print(f"[OK] Configured sync clients: {', '.join(configured_clients)}")

        print("\n[PASS] All tests passed! Dependency injection is working correctly.")

    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}")
        import traceback
        traceback.print_exc()
        raise

    finally:
        # Close database connection to prevent file locks
        if 'container' in locals():
            try:
                db_service = container.database_service()
                if hasattr(db_service, 'db_manager'):
                    db_service.db_manager.close()
                    # print("[INFO] Database connection closed.")
            except Exception as e:
                print(f"[WARN] Failed to close DB connection: {e}")

        # Close logging handlers to prevent file locks on logs/unified_app.log
        import logging
        logging.shutdown()
        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)

        # Cleanup
        import shutil
        if Path('test_data').exists():
            shutil.rmtree('test_data')
        if Path('test_books').exists():
            shutil.rmtree('test_books')


if __name__ == '__main__':
    test_dependency_injection()
