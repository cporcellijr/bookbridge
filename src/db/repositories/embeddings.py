import json
from datetime import timedelta
from typing import List
from ..models import EmbeddingCache
from src.utils.time_utils import utcnow
import logging

logger = logging.getLogger('src.db.database_service')

class EmbeddingsMixin:
    """Database operations for embeddings."""

    def get_cached_embeddings(self, model: str, text_hashes: List[str]) -> dict:
        """Return {text_hash: vector} for cached embeddings of `model`."""
        if not model or not text_hashes:
            return {}
        result = {}
        with self.get_session() as session:
            rows = session.query(EmbeddingCache).filter(
                EmbeddingCache.model == model,
                EmbeddingCache.text_hash.in_(text_hashes),
            ).all()
            for row in rows:
                try:
                    vector = json.loads(row.vector_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(vector, list):
                    result[row.text_hash] = vector
        return result

    def save_cached_embeddings(self, model: str, vectors_by_hash: dict) -> None:
        """Insert embeddings for `model`, ignoring hashes that already exist."""
        if not model or not vectors_by_hash:
            return
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        rows = [
            {"model": model, "text_hash": h, "vector_json": json.dumps(v), "created_at": utcnow()}
            for h, v in vectors_by_hash.items()
            if h and isinstance(v, list)
        ]
        if not rows:
            return
        with self.get_session() as session:
            stmt = sqlite_insert(EmbeddingCache.__table__).values(rows)
            session.execute(stmt.on_conflict_do_nothing(index_elements=["model", "text_hash"]))

    def prune_embedding_cache(self, keep_model: str, max_age_days: int = 90) -> int:
        """Drop rows for other models and rows older than `max_age_days`. Returns count."""
        cutoff = utcnow() - timedelta(days=max_age_days)
        with self.get_session() as session:
            query = session.query(EmbeddingCache).filter(
                (EmbeddingCache.model != keep_model) | (EmbeddingCache.created_at < cutoff)
            )
            count = query.delete(synchronize_session=False)
        if count:
            logger.info(f"Pruned {count} stale embedding cache rows")
        return count
