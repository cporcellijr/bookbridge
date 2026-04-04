"""
Migration Service.
Handles legacy data migration from file-based storage to SQLite.
"""

import logging
import json
from pathlib import Path

from src.services.alignment_service import AlignmentService
from src.db.models import BookAlignment, BookloreBook

logger = logging.getLogger(__name__)

class MigrationService:
    def __init__(self, database_service, alignment_service: AlignmentService, data_dir: Path):
        self.database_service = database_service
        self.alignment_service = alignment_service
        self.data_dir = data_dir
        self.transcripts_dir = data_dir / "transcripts"

    def migrate_legacy_data(self):
        """
        Migrate legacy JSON transcript files to the database.
        
        Strategy:
        1. Look for *.json in transcripts/
        2. Check if we already have an entry in 'book_alignments' table.
        3. If not, we can't easily "align" without the book text!
           Legacy files only contain the transcript segments.
           
           Option A: Load the transcript into a temporary structure? No, we want unified structure.
        Migrate all legacy JSON data to database:
        1. Transcripts/Alignments
        2. Grimmory Cache
        3. Clean up obsolete files
        """
        self._migrate_alignments()
        self._migrate_booklore_cache()
        self._cleanup_legacy_files()

    def _migrate_alignments(self):
        """Migrate alignment JSONs to DB."""
        if not self.transcripts_dir.exists():
            return

        try:
            # Find all alignment files
            files = list(self.transcripts_dir.glob("*_alignment.json"))
            if not files:
                return

            logger.info(f"🔄 Found {len(files)} legacy alignment files to migrate...")
            
            count = 0
            with self.database_service.get_session() as session:
                for map_file in files:
                    # Extract ABS ID from filename (format: {abs_id}_alignment.json)
                    abs_id = map_file.name.replace("_alignment.json", "")
                    
                    # Check if already exists in DB
                    from sqlalchemy import text
                    existing = session.query(BookAlignment).filter(BookAlignment.abs_id == abs_id).first()
                    if existing:
                        continue

                    try:
                        with open(map_file, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            
                        # Create new DB entry
                        # Convert list of dicts to JSON string
                        new_entry = BookAlignment(abs_id=abs_id, alignment_map_json=json.dumps(data))
                        session.add(new_entry)
                        count += 1
                    except Exception as e:
                        logger.error(f"❌ Failed to migrate '{map_file.name}': {e}")

                # session.commit() is handled by context manager
            
            if count > 0:
                logger.info(f"✅ Migrated {count} alignment maps to database")
            
        except Exception as e:
            logger.error(f"❌ Migration error: {e}")

    def _migrate_booklore_cache(self):
        """Migrate booklore_cache.json to booklore_books table."""
        cache_file = self.data_dir / "booklore_cache.json"
        if not cache_file.exists():
            return

        # Check if we've already migrated (simple check: is table empty?)
        # A more robust check might be needed if we support partial migrations, 
        # but for now we assume if DB has data, we are good.
        existing_count = len(self.database_service.get_all_booklore_books())
        if existing_count > 0:
            return

        logger.info("📦 Migrating Grimmory cache to database...")
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            books = data.get('books', {})
            count = 0
            
            for filename, info in books.items():
                try:
                    # Convert to model
                    b = BookloreBook(
                        filename=filename.lower(), # Normalize key
                        title=info.get('title'),
                        authors=info.get('authors'),
                        raw_metadata=json.dumps(info)
                    )
                    self.database_service.save_booklore_book(b)
                    count += 1
                except Exception as e:
                    logger.warning(f"⚠️ Failed to migrate book '{filename}': {e}")
            
            if count > 0:
                logger.info(f"✅ Migrated {count} Grimmory books to database")
                # Rename to .bak to prevent re-reading and confusion
                try:
                    cache_file.rename(cache_file.with_suffix('.json.bak'))
                    logger.info("📦 Renamed legacy booklore_cache.json to .bak")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to rename legacy cache file: {e}")

        except Exception as e:
            logger.error(f"❌ Grimmory migration failed: {e}")

    def _cleanup_legacy_files(self):
        """Identify and optionally rename/delete obsolete JSON files."""
        legacy_files = [
            "kosync_hash_cache.json",
            "mapping_db.json",
            "last_state.json",
            "settings.json"
        ]
        
        for fname in legacy_files:
            fpath = self.data_dir / fname
            if fpath.exists():
                try:
                    # Renaming to .bak allows user recovery if needed
                    bak_path = fpath.with_suffix('.json.bak')
                    if not bak_path.exists():
                        fpath.rename(bak_path)
                        logger.info(f"🧹 Renamed legacy file {fname} to .bak")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to cleanup '{fname}': {e}")

    def _delete_legacy_file(self, file_path: Path):
        """Delete legacy file after successful migration."""
        try:
            file_path.unlink()
            logger.debug(f"Deleted legacy file: {file_path.name}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to delete legacy file '{file_path.name}': {e}")
