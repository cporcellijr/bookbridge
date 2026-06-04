import os
import re
from typing import Callable, List, Set, Any, Dict, Optional


class SuggestionsService:
    """Service for scanning unmatched audiobooks and producing ebook suggestions."""

    def __init__(
        self,
        database_service,
        container,
        manager,
        get_audiobooks_conditionally: Callable[[], List[dict]],
        get_searchable_ebooks: Callable[[str], List[Any]],
        audiobook_matches_search: Callable[[dict, str], bool],
        get_abs_author: Callable[[dict], str],
        logger,
        calibre_identifier_resolver: Optional[Any] = None,
        ollama_client: Optional[Any] = None,
    ):
        self.database_service = database_service
        self.container = container
        self.manager = manager
        self.get_audiobooks_conditionally = get_audiobooks_conditionally
        self.get_searchable_ebooks = get_searchable_ebooks
        self.audiobook_matches_search = audiobook_matches_search
        self.get_abs_author = get_abs_author
        self.logger = logger
        self.calibre_identifier_resolver = calibre_identifier_resolver
        self.ollama_client = ollama_client
        # Per-scan embedding cache (text -> vector); a fresh service is built per scan.
        self._ollama_embed_cache: Dict[str, Any] = {}

    @staticmethod
    def _candidate_title(candidate: Any) -> str:
        return (
            getattr(candidate, 'title', None)
            or getattr(candidate, 'stem', None)
            or getattr(candidate, 'name', '')
            or ''
        ).strip()

    @staticmethod
    def _candidate_author(candidate: Any) -> str:
        return (getattr(candidate, 'authors', None) or '').strip()

    def _build_ebook_candidate_pool(self) -> List[dict]:
        """
        Build searchable ebook candidates once per scan to avoid per-book provider calls.
        """
        try:
            candidates = self.get_searchable_ebooks('')
        except Exception as e:
            self.logger.warning(f"Suggestion scan failed to load candidate ebook pool: {e}")
            return []

        return self._prepare_candidate_pool(candidates)

    def _prepare_candidate_pool(self, candidates: List[Any]) -> List[dict]:
        prepared = []
        resolver = self.calibre_identifier_resolver
        resolver_enabled = bool(resolver and resolver.is_enabled())

        for candidate in candidates or []:
            candidate_title = self._candidate_title(candidate)
            if not candidate_title:
                continue

            candidate_author = self._candidate_author(candidate)
            candidate_source = (getattr(candidate, 'source', None) or '').strip()
            source_id = getattr(candidate, 'source_id', None) or getattr(candidate, 'booklore_id', None)

            abs_identifier = getattr(candidate, 'abs_identifier', None)
            if not abs_identifier and resolver_enabled and candidate_source.upper() == 'CWA' and source_id:
                try:
                    abs_identifier = resolver.get_abs_id(source_id)
                except Exception as e:
                    self.logger.debug(f"Calibre identifier lookup failed for {source_id}: {e}")

            prepared.append({
                "title": candidate_title,
                "author": candidate_author,
                "source": candidate_source,
                "source_id": source_id,
                "search_text": f"{candidate_title} {candidate_author}".strip(),
                "name": getattr(candidate, 'name', ''),
                "display_name": getattr(candidate, 'display_name', None) or getattr(candidate, 'name', ''),
                "abs_identifier": abs_identifier,
            })

        return prepared

    @staticmethod
    def _build_bridge_key(audio_source: str, audio_source_id: str) -> str:
        if not audio_source_id:
            return ""
        source_id = str(audio_source_id).strip()
        if not source_id:
            return ""
        if source_id.lower().startswith("booklore:"):
            return f"booklore:{source_id.split(':', 1)[1].strip()}"

        source_name = str(audio_source or "").strip().lower()
        if source_name == "booklore":
            return f"booklore:{source_id}"
        return source_id

    def _audio_source(self, ab: dict) -> str:
        source = (ab.get("audio_source") or ab.get("source") or "ABS")
        source_text = str(source).strip()
        return source_text or "ABS"

    def _audio_source_id(self, ab: dict) -> str:
        raw_id = ab.get("audio_source_id") or ab.get("source_id") or ab.get("id")
        if raw_id is None:
            return ""
        value = str(raw_id).strip()
        if not value:
            return ""
        if value.lower().startswith("booklore:"):
            return value.split(":", 1)[1].strip()
        return value

    def _audio_bridge_key(self, ab: dict) -> str:
        explicit = (ab.get("bridge_key") or "").strip()
        if explicit:
            return explicit

        source = self._audio_source(ab)
        source_id = self._audio_source_id(ab)
        return self._build_bridge_key(source, source_id)

    def _audio_title(self, ab: dict) -> str:
        title = (ab.get("audio_title") or ab.get("title") or "").strip()
        if title:
            return title
        try:
            return (self.manager.get_abs_title(ab) or "").strip()
        except Exception:
            return ""

    def _audio_author(self, ab: dict) -> str:
        author = (
            ab.get("audio_author")
            or ab.get("authors")
            or ab.get("author")
            or ""
        )
        author_text = str(author).strip()
        if author_text:
            return author_text
        try:
            return (self.get_abs_author(ab) or "").strip()
        except Exception:
            return ""

    def _audio_duration(self, ab: dict):
        raw_duration = ab.get("audio_duration")
        if raw_duration is None:
            raw_duration = ab.get("duration")
        if raw_duration is None:
            try:
                raw_duration = self.manager.get_duration(ab)
            except Exception:
                raw_duration = None
        try:
            return float(raw_duration) if raw_duration is not None else None
        except (TypeError, ValueError):
            return None

    def _audio_cover_url(self, ab: dict, audio_source: str, audio_source_id: str) -> str:
        cover_url = (ab.get("audio_cover_url") or ab.get("cover_url") or "").strip()
        if cover_url:
            return cover_url
        if audio_source == "ABS" and audio_source_id:
            abs_client = self.container.abs_client()
            return f"{abs_client.base_url}/api/items/{audio_source_id}/cover?token={abs_client.token}"
        return ""

    def _audiobook_matches_candidate(
        self,
        ab: dict,
        candidate_search_text: str,
        audio_title: str,
        audio_author: str,
    ) -> bool:
        if not candidate_search_text:
            return False

        if ab.get("media") is not None:
            try:
                return bool(self.audiobook_matches_search(ab, candidate_search_text))
            except Exception:
                return False

        audio_text = f"{audio_title} {audio_author}".strip().lower()
        candidate_text = candidate_search_text.lower()
        if not audio_text:
            return False
        return candidate_text in audio_text or audio_text in candidate_text

    def get_ignored_suggestion_source_ids(self) -> Set[str]:
        """Return source IDs (bridge keys) that are marked as ignored."""
        if hasattr(self.database_service, 'get_ignored_suggestion_source_ids'):
            try:
                return set(self.database_service.get_ignored_suggestion_source_ids() or [])
            except Exception as e:
                self.logger.warning(f"Could not load ignored suggestions via service method: {e}")

        if not hasattr(self.database_service, 'get_session'):
            return set()

        from src.db.models import PendingSuggestion

        ignored_source_ids = set()
        try:
            with self.database_service.get_session() as db_session:
                rows = db_session.query(PendingSuggestion.source_id).filter(
                    PendingSuggestion.status == 'ignored'
                ).all()
                ignored_source_ids = {row[0] for row in rows if row and row[0]}
        except Exception as e:
            self.logger.warning(f"Could not load ignored suggestions: {e}")

        return ignored_source_ids

    def _find_authoritative_match(
        self,
        candidate_pool: List[dict],
        audio_source_id: str,
    ) -> Optional[dict]:
        """If any candidate's audiobookshelf_id identifier matches the audio source,
        return a 100-score match dict. Else None."""
        if not audio_source_id:
            return None
        target = str(audio_source_id).strip()
        if not target:
            return None

        for candidate_info in candidate_pool:
            ident = candidate_info.get("abs_identifier")
            if ident and str(ident).strip() == target:
                return {
                    "ebook_filename": candidate_info["name"],
                    "display_name": candidate_info["display_name"],
                    "author": candidate_info.get("author", ""),
                    "source": candidate_info.get("source", ""),
                    "source_id": candidate_info.get("source_id"),
                    "score": 100.0,
                }
        return None

    def _scan_single_audiobook(self, ab: dict, candidate_pool: List[dict]) -> Optional[dict]:
        """Scan one unmatched audiobook and return a suggestion dict or None."""
        from rapidfuzz import fuzz

        audio_source = self._audio_source(ab)
        audio_source_id = self._audio_source_id(ab)
        bridge_key = self._audio_bridge_key(ab)
        audio_title = self._audio_title(ab)
        audio_author = self._audio_author(ab)

        if not bridge_key or not audio_title:
            return None

        per_book_pool = candidate_pool
        if not per_book_pool:
            # Fallback keeps prior behavior in source setups where no shared pool is available.
            try:
                per_book_pool = self._prepare_candidate_pool(self.get_searchable_ebooks(audio_title))
            except Exception as e:
                self.logger.warning(f"Suggestion scan failed to search ebooks for '{audio_title}': {e}")
                return None

        authoritative = self._find_authoritative_match(per_book_pool, audio_source_id)
        if authoritative is not None:
            self.logger.info(
                f"📌 Authoritative match via audiobookshelf_id for '{audio_title}' "
                f"-> {authoritative.get('display_name') or authoritative.get('name')}"
            )
            audio_cover_url = self._audio_cover_url(ab, audio_source, audio_source_id)
            audio_duration = self._audio_duration(ab)
            return {
                "bridge_key": bridge_key,
                "audio_source": audio_source,
                "audio_source_id": audio_source_id,
                "audio_title": audio_title,
                "audio_author": audio_author,
                "audio_duration": audio_duration,
                "audio_cover_url": audio_cover_url,
                "audio_provider_book_id": str(ab.get("audio_provider_book_id") or audio_source_id or ""),
                "audio_provider_file_id": str(ab.get("audio_provider_file_id") or ""),
                "abs_id": bridge_key,
                "abs_title": audio_title,
                "abs_author": audio_author,
                "duration": audio_duration,
                "cover_url": audio_cover_url,
                "matches": [authoritative],
            }

        matches = []
        for candidate_info in per_book_pool:
            candidate_title = candidate_info["title"]
            candidate_author = candidate_info["author"]
            candidate_source = candidate_info.get("source", "")

            title_score = float(fuzz.token_sort_ratio(audio_title, candidate_title))
            if audio_author:
                author_score = float(fuzz.token_sort_ratio(audio_author, candidate_author)) if candidate_author else 0.0
                score = (title_score * 0.7) + (author_score * 0.3)
            else:
                score = title_score

            if score < 60:
                continue

            candidate_search_text = candidate_info["search_text"]
            direct_match = self._audiobook_matches_candidate(
                ab,
                candidate_search_text,
                audio_title,
                audio_author,
            )

            matches.append({
                "ebook_filename": candidate_info["name"],
                "display_name": candidate_info["display_name"],
                "author": candidate_author,
                "source": candidate_source,
                "source_id": candidate_info.get("source_id"),
                "score": round(score, 1),
                "_direct_match": direct_match
            })

        if not matches:
            return None

        matches.sort(key=lambda m: (m.get('score', 0), 1 if m.get('_direct_match') else 0), reverse=True)
        for m in matches:
            m.pop('_direct_match', None)

        matches = self._apply_ollama_reranking(audio_title, audio_author, matches)

        audio_cover_url = self._audio_cover_url(ab, audio_source, audio_source_id)
        audio_duration = self._audio_duration(ab)
        return {
            "bridge_key": bridge_key,
            "audio_source": audio_source,
            "audio_source_id": audio_source_id,
            "audio_title": audio_title,
            "audio_author": audio_author,
            "audio_duration": audio_duration,
            "audio_cover_url": audio_cover_url,
            "audio_provider_book_id": str(ab.get("audio_provider_book_id") or audio_source_id or ""),
            "audio_provider_file_id": str(ab.get("audio_provider_file_id") or ""),
            # Legacy aliases kept for template/session compatibility.
            "abs_id": bridge_key,
            "abs_title": audio_title,
            "abs_author": audio_author,
            "duration": audio_duration,
            "cover_url": audio_cover_url,
            "matches": matches,
        }

    # --- Ollama-assisted re-ranking (optional, gated) -------------------------

    @staticmethod
    def _env_true(key: str) -> bool:
        return os.environ.get(key, "false").lower() == "true"

    @staticmethod
    def _env_float(key: str, default: float) -> float:
        try:
            return float(os.environ.get(key, default))
        except (TypeError, ValueError):
            return default

    def _ollama_ready(self) -> bool:
        client = self.ollama_client
        return bool(client and client.is_configured())

    def _embed_texts(self, texts: List[str]) -> Optional[Dict[str, Any]]:
        """Embed texts (using the per-scan cache). Returns {text: vector} or None on failure."""
        cache = self._ollama_embed_cache
        unique = [t for t in dict.fromkeys(texts) if t]
        missing = [t for t in unique if t not in cache]
        if missing:
            vectors = self.ollama_client.embed(missing)
            if vectors is None:
                return None
            for text, vec in zip(missing, vectors):
                cache[text] = vec
        return {t: cache.get(t) for t in texts}

    def _apply_ollama_reranking(
        self, audio_title: str, audio_author: str, matches: List[dict]
    ) -> List[dict]:
        """Run the optional embedding re-rank → judge → file-resolution pipeline."""
        if not matches or not self._ollama_ready():
            return matches

        matches = self._ollama_rerank_band(audio_title, audio_author, matches)
        matches = self._ollama_judge_and_resolve(audio_title, audio_author, matches)
        return matches

    def _ollama_rerank_band(
        self, audio_title: str, audio_author: str, matches: List[dict]
    ) -> List[dict]:
        """Stage 1: re-rank the borderline score band by semantic (embedding) similarity."""
        if not self._env_true("OLLAMA_RERANK_SUGGESTIONS"):
            return matches

        band_min = self._env_float("OLLAMA_RERANK_BAND_MIN", 60.0)
        band_max = self._env_float("OLLAMA_RERANK_BAND_MAX", 95.0)

        above = [m for m in matches if m.get("score", 0) > band_max]
        band = [m for m in matches if band_min <= m.get("score", 0) <= band_max]
        below = [m for m in matches if m.get("score", 0) < band_min]
        if len(band) < 2:
            return matches

        audio_text = f"{audio_title} {audio_author}".strip()
        cand_texts = [
            f"{m.get('display_name') or ''} {m.get('author') or ''}".strip() for m in band
        ]
        embedded = self._embed_texts([audio_text] + cand_texts)
        if embedded is None:
            self.logger.info("Ollama re-rank skipped (embedding unavailable)")
            return matches
        audio_vec = embedded.get(audio_text)
        if not audio_vec:
            return matches

        from src.api.ollama_client import cosine_similarity

        for m, ct in zip(band, cand_texts):
            cand_vec = embedded.get(ct)
            cos = cosine_similarity(audio_vec, cand_vec) if cand_vec else 0.0
            m["score"] = round(0.6 * m.get("score", 0) + 40.0 * cos, 1)

        above.sort(key=lambda m: m.get("score", 0), reverse=True)
        band.sort(key=lambda m: m.get("score", 0), reverse=True)
        return above + band + below

    def _ollama_judge_and_resolve(
        self, audio_title: str, audio_author: str, matches: List[dict]
    ) -> List[dict]:
        """Stage 2: judge ambiguous top-2; Stage 3: resolve the real file on a confident verdict."""
        if not self._env_true("OLLAMA_JUDGE_SUGGESTIONS") or len(matches) < 2:
            return matches

        margin = self._env_float("OLLAMA_JUDGE_MARGIN", 5.0)
        if (matches[0].get("score", 0) - matches[1].get("score", 0)) > margin:
            return matches  # top match is already clear; don't spend a chat call

        candidates = matches[:3]
        lines = [
            f"{i}. title: {m.get('display_name') or ''} | author: {m.get('author') or ''}"
            for i, m in enumerate(candidates)
        ]
        prompt = (
            "You are matching an audiobook to its ebook edition. Decide which candidate "
            "ebook is the SAME WORK as the audiobook (same book, any edition or translation), "
            "or none of them.\n"
            f"Audiobook: title: {audio_title} | author: {audio_author}\n"
            "Candidate ebooks:\n" + "\n".join(lines) + "\n"
            'Respond ONLY with JSON: {"choice": <candidate number or null>, '
            '"confidence": <integer 0-100>, "reason": "<short>"}'
        )
        result = self.ollama_client.judge(prompt)
        if not isinstance(result, dict):
            return matches

        choice = result.get("choice")
        try:
            confidence = float(result.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        if not isinstance(choice, int) or not (0 <= choice < len(candidates)):
            return matches

        chosen = candidates[choice]
        self.logger.info(
            f"🧠 Ollama judge picked '{chosen.get('display_name')}' for '{audio_title}' "
            f"(confidence {confidence:.0f})"
        )
        # Pin the chosen match to the top.
        matches = [chosen] + [m for m in matches if m is not chosen]

        if confidence >= self._env_float("OLLAMA_JUDGE_CONFIDENCE_MIN", 85.0):
            self._resolve_real_file(audio_title, chosen)
        return matches

    def _resolve_real_file(self, audio_title: str, chosen: dict) -> None:
        """Stage 3: targeted search to fill in the authoritative file the light pool omitted."""
        from rapidfuzz import fuzz

        try:
            candidates = self.get_searchable_ebooks(audio_title)
        except Exception as e:
            self.logger.warning(f"Ollama file-resolution search failed for '{audio_title}': {e}")
            return

        pool = self._prepare_candidate_pool(candidates)
        if not pool:
            return

        target = f"{chosen.get('display_name') or ''} {chosen.get('author') or ''}".strip()
        best = None
        best_score = 0.0
        for cand in pool:
            score = float(fuzz.token_sort_ratio(target, cand.get("search_text", "")))
            if score > best_score:
                best_score = score
                best = cand

        if best and best_score >= 80 and best.get("name"):
            # Volume guard: a base title ("Heretic Spellblade") fuzzy-matches its sequel
            # ("Heretic Spellblade 2") well above threshold. Refuse to attach the wrong
            # volume's file — the audiobook title carries the authoritative volume number.
            audio_vol = self._trailing_volume(audio_title)
            cand_vol = self._trailing_volume(best.get("title"))
            if audio_vol != cand_vol:
                self.logger.info(
                    f"🔎 Skipped file resolution for '{audio_title}': volume mismatch "
                    f"(audio vol={audio_vol}, candidate '{best.get('title')}' vol={cand_vol})"
                )
                return
            chosen["ebook_filename"] = best["name"]
            if best.get("source_id"):
                chosen["source_id"] = best["source_id"]
            if best.get("source"):
                chosen["source"] = best["source"]
            self.logger.info(
                f"🔎 Resolved real file for '{audio_title}' -> {best['name']} (match {best_score:.0f})"
            )

    @staticmethod
    def _trailing_volume(title: Optional[str]) -> Optional[str]:
        """Extract a trailing volume number, ignoring trailing parentheticals like '(Unabridged)'."""
        text = (title or "").strip()
        text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
        match = re.search(r"(\d+)\s*$", text)
        return match.group(1) if match else None

    def _build_audiobook_candidate_pool(self) -> List[dict]:
        """Build searchable audiobook candidates once per shelf-watch scan.

        Mirrors `_build_ebook_candidate_pool` but inverted: callers anchor on a
        known ebook and we score audiobook candidates against it.
        """
        try:
            audiobooks = self.get_audiobooks_conditionally()
        except Exception as e:
            self.logger.warning(f"Shelf-watch scan failed to load audiobook candidate pool: {e}")
            return []

        raw_count = len(audiobooks) if audiobooks else 0
        prepared = []
        skipped_no_bridge = 0
        skipped_no_title = 0
        for ab in audiobooks or []:
            audio_source = self._audio_source(ab)
            audio_source_id = self._audio_source_id(ab)
            bridge_key = self._audio_bridge_key(ab)
            audio_title = self._audio_title(ab)
            if not bridge_key:
                skipped_no_bridge += 1
                continue
            if not audio_title:
                skipped_no_title += 1
                continue
            audio_author = self._audio_author(ab)
            audio_duration = self._audio_duration(ab)
            audio_cover_url = self._audio_cover_url(ab, audio_source, audio_source_id)
            prepared.append({
                "audio_source": audio_source,
                "audio_source_id": audio_source_id,
                "bridge_key": bridge_key,
                "audio_title": audio_title,
                "audio_author": audio_author,
                "audio_duration": audio_duration,
                "audio_cover_url": audio_cover_url,
                "audio_provider_book_id": str(ab.get("audio_provider_book_id") or audio_source_id or ""),
                "audio_provider_file_id": str(ab.get("audio_provider_file_id") or ""),
                "search_text": f"{audio_title} {audio_author}".strip(),
            })
        self.logger.info(
            "Shelf-watch candidate pool: raw=%d kept=%d skipped_no_bridge=%d skipped_no_title=%d",
            raw_count, len(prepared), skipped_no_bridge, skipped_no_title,
        )
        return prepared

    @staticmethod
    def _ebook_anchor_fields(ebook: dict) -> dict:
        """Extract title/author/filename/id from a Grimmory ebook dict (lenient on key names)."""
        title = (ebook.get("title") or ebook.get("name") or "").strip()
        author = (
            ebook.get("author")
            or ebook.get("authors")
            or ""
        )
        if isinstance(author, list):
            author = ", ".join(str(a) for a in author if a)
        author = str(author or "").strip()
        filename = (ebook.get("filename") or ebook.get("fileName") or ebook.get("name") or "").strip()
        grimmory_id = ebook.get("grimmory_id") or ebook.get("id") or ebook.get("book_id")
        if grimmory_id is not None:
            grimmory_id = str(grimmory_id).strip()
        return {
            "title": title,
            "author": author,
            "filename": filename,
            "grimmory_id": grimmory_id or "",
        }

    def _scan_single_ebook(self, ebook: dict, candidate_pool: List[dict]) -> Optional[dict]:
        """Reverse counterpart of `_scan_single_audiobook` for the shelf-watch flow.

        Takes a Grimmory ebook as the anchor and scans audiobook candidates,
        applying the same `rapidfuzz.fuzz.token_sort_ratio` scoring formula and
        60-point floor. Returns None when no candidate clears the floor.
        """
        from rapidfuzz import fuzz

        anchor = self._ebook_anchor_fields(ebook)
        if not anchor["title"]:
            self.logger.debug(
                f"Shelf-watch scan: skipping '{anchor.get('filename')}' — no anchor title"
            )
            return None

        if not candidate_pool:
            self.logger.debug(
                f"Shelf-watch scan: '{anchor['title']}' — candidate pool empty"
            )
            return None

        ebook_title = anchor["title"]
        ebook_author = anchor["author"]

        matches = []
        best_overall = None  # Track the highest-scoring candidate even if below the floor.
        for cand in candidate_pool:
            cand_title = cand.get("audio_title") or ""
            cand_author = cand.get("audio_author") or ""

            title_score = float(fuzz.token_sort_ratio(ebook_title, cand_title))
            if ebook_author:
                author_score = float(fuzz.token_sort_ratio(ebook_author, cand_author)) if cand_author else 0.0
                score = (title_score * 0.7) + (author_score * 0.3)
            else:
                score = title_score

            if best_overall is None or score > best_overall[0]:
                best_overall = (score, cand_title, cand_author)

            if score < 60:
                continue

            matches.append({
                "audio_source": cand.get("audio_source", ""),
                "audio_source_id": cand.get("audio_source_id", ""),
                "bridge_key": cand.get("bridge_key", ""),
                "audio_title": cand_title,
                "audio_author": cand_author,
                "audio_duration": cand.get("audio_duration"),
                "audio_cover_url": cand.get("audio_cover_url", ""),
                "audio_provider_book_id": cand.get("audio_provider_book_id", ""),
                "audio_provider_file_id": cand.get("audio_provider_file_id", ""),
                "score": round(score, 1),
            })

        if not matches:
            if best_overall is not None:
                self.logger.info(
                    f"Shelf-watch scan: '{ebook_title}' / '{ebook_author}' — "
                    f"no audio above 60-point floor. Closest: '{best_overall[1]}' / "
                    f"'{best_overall[2]}' score={round(best_overall[0], 1)}"
                )
            else:
                self.logger.info(
                    f"Shelf-watch scan: '{ebook_title}' — pool empty after candidate filtering"
                )
            return None

        matches.sort(key=lambda m: m.get("score", 0), reverse=True)
        return {
            "ebook_anchor": anchor,
            "matches": matches,
        }

    def scan_library_suggestions(
        self,
        cached_suggestions_by_abs: Optional[Dict[str, dict]] = None,
        cached_no_match_abs_ids: Optional[List[str]] = None,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """Scan unmatched audiobooks, reusing cache and only scanning newly unmatched IDs."""
        cached_suggestions_by_abs = dict(cached_suggestions_by_abs or {})
        cached_no_match_abs_ids_set = set(cached_no_match_abs_ids or [])

        def emit_progress(
            phase: str,
            percent: float,
            message: str,
            scanned_new_done: int,
            scanned_new_total: int,
            reused_cached: int,
            total_unmatched: int,
        ):
            if not progress_callback:
                return
            try:
                progress_callback({
                    "phase": phase,
                    "percent": int(max(0, min(100, round(percent)))),
                    "message": message,
                    "scanned_new_done": scanned_new_done,
                    "scanned_new_total": scanned_new_total,
                    "reused_cached": reused_cached,
                    "total_unmatched": total_unmatched,
                })
            except Exception:
                pass

        try:
            all_audiobooks = self.get_audiobooks_conditionally()
        except Exception as e:
            self.logger.error(f"Failed to load audiobooks for suggestions scan: {e}")
            return {
                "suggestions": [],
                "cache_by_abs": {},
                "no_match_abs_ids": [],
                "stats": {"scanned_new": 0, "reused_cached": 0, "total_unmatched": 0}
            }

        matched_bridge_keys = set()
        for book in self.database_service.get_all_books():
            abs_id = getattr(book, "abs_id", None)
            if abs_id:
                matched_bridge_keys.add(str(abs_id))
            mapped_audio_source = getattr(book, "audio_source", None) or "ABS"
            mapped_audio_source_id = getattr(book, "audio_source_id", None)
            if mapped_audio_source_id:
                mapped_key = self._build_bridge_key(str(mapped_audio_source), str(mapped_audio_source_id))
                if mapped_key:
                    matched_bridge_keys.add(mapped_key)

        ignored_source_ids = self.get_ignored_suggestion_source_ids()

        unmatched_audiobooks = []
        for ab in all_audiobooks:
            bridge_key = self._audio_bridge_key(ab)
            if not bridge_key:
                continue
            if bridge_key in matched_bridge_keys:
                continue
            if bridge_key in ignored_source_ids:
                continue
            unmatched_audiobooks.append((bridge_key, ab))

        unmatched_abs_ids = {bridge_key for bridge_key, _ab in unmatched_audiobooks}

        # Keep only cache entries still relevant to current unmatched universe.
        cache_by_abs = {
            abs_id: suggestion
            for abs_id, suggestion in cached_suggestions_by_abs.items()
            if abs_id in unmatched_abs_ids and abs_id not in ignored_source_ids
        }
        no_match_abs_ids_set = {
            abs_id for abs_id in cached_no_match_abs_ids_set
            if abs_id in unmatched_abs_ids and abs_id not in ignored_source_ids
        }
        reused_cached_count = len(cache_by_abs) + len(no_match_abs_ids_set)

        new_scan_candidates = [
            (bridge_key, ab) for bridge_key, ab in unmatched_audiobooks
            if bridge_key not in cache_by_abs and bridge_key not in no_match_abs_ids_set
        ]

        total_unmatched = len(unmatched_abs_ids)
        scanned_new_total = len(new_scan_candidates)
        scanned_new_done = 0

        if total_unmatched == 0:
            emit_progress(
                phase="finalizing",
                percent=100,
                message="No unmatched audiobooks to scan",
                scanned_new_done=0,
                scanned_new_total=0,
                reused_cached=reused_cached_count,
                total_unmatched=0,
            )
        else:
            initial_percent = (reused_cached_count / total_unmatched) * 100
            if scanned_new_total > 0:
                msg = f"Scanning 0/{scanned_new_total} new audiobooks..."
            else:
                msg = "All unmatched audiobooks served from cache"
            emit_progress(
                phase="scanning",
                percent=initial_percent,
                message=msg,
                scanned_new_done=0,
                scanned_new_total=scanned_new_total,
                reused_cached=reused_cached_count,
                total_unmatched=total_unmatched,
            )

        candidate_pool = []
        if scanned_new_total > 0:
            emit_progress(
                phase="loading_candidates",
                percent=(reused_cached_count / total_unmatched) * 100 if total_unmatched else 0,
                message="Loading ebook candidates...",
                scanned_new_done=0,
                scanned_new_total=scanned_new_total,
                reused_cached=reused_cached_count,
                total_unmatched=total_unmatched,
            )
            candidate_pool = self._build_ebook_candidate_pool()
            self.logger.info(
                "Suggestions scan candidate pool loaded: %s ebooks for %s new audiobooks",
                len(candidate_pool),
                scanned_new_total,
            )

        for idx, (bridge_key, ab) in enumerate(new_scan_candidates, start=1):
            suggestion = self._scan_single_audiobook(ab, candidate_pool)
            if suggestion:
                cache_by_abs[bridge_key] = suggestion
                no_match_abs_ids_set.discard(bridge_key)
            else:
                no_match_abs_ids_set.add(bridge_key)
            scanned_new_done = idx
            processed_total = reused_cached_count + scanned_new_done
            percent = (processed_total / total_unmatched) * 100 if total_unmatched else 100
            emit_progress(
                phase="scanning",
                percent=percent,
                message=f"Scanning {scanned_new_done}/{scanned_new_total} new audiobooks...",
                scanned_new_done=scanned_new_done,
                scanned_new_total=scanned_new_total,
                reused_cached=reused_cached_count,
                total_unmatched=total_unmatched,
            )

        suggestions = list(cache_by_abs.values())
        suggestions.sort(key=lambda s: s.get('matches', [{}])[0].get('score', 0), reverse=True)

        emit_progress(
            phase="finalizing",
            percent=100,
            message=f"Scan complete. {len(suggestions)} suggestions ready.",
            scanned_new_done=scanned_new_total,
            scanned_new_total=scanned_new_total,
            reused_cached=reused_cached_count,
            total_unmatched=total_unmatched,
        )

        return {
            "suggestions": suggestions,
            "cache_by_abs": cache_by_abs,
            "no_match_abs_ids": sorted(no_match_abs_ids_set),
            "stats": {
                "scanned_new": len(new_scan_candidates),
                "reused_cached": reused_cached_count,
                "total_unmatched": total_unmatched,
                "candidate_pool_size": len(candidate_pool),
            },
        }
