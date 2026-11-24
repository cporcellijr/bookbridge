import re
from datetime import datetime
from typing import Optional
from ..models import KoreaderAnnotation, KoreaderAnnotationDeviceState
from src.utils.time_utils import utcnow
import logging

logger = logging.getLogger('src.db.database_service')

class AnnotationsMixin:
    """Database operations for annotations."""

    @staticmethod
    def _normalize_xpointer_for_key(pos0) -> str:
        """Canonicalize a KOReader xpointer so the identity key is stable across
        the create -> transmit -> apply -> re-read cycle.

        crengine re-serializes xpointers when it re-validates an externally
        applied annotation (dropping a trailing ``.0`` text offset, adding or
        stripping ``[1]`` sibling indexes), so the raw pos0 differs between the
        authoring device and a receiving device for the SAME highlight. Keying
        identity on the raw pos0 makes a receiving device's re-serialized copy
        look like a different (missing) annotation, which the deletion detector
        then tombstones. Normalizing identically on the device AND the bridge
        keeps the key stable. Mirrors the KOReader convention used by the
        Grimmory plugin. The RAW pos0 is preserved separately for positioning —
        this is used ONLY for the identity hash.
        """
        if not pos0:
            return ""
        text = str(pos0).replace("[1]", "")
        if text.endswith(".0"):
            text = text[:-2]
        return text

    @staticmethod
    def _bookorbit_annotation_key(datetime_str: str, pos0: str) -> str:
        """Key in BookOrbit's koplugin convention: md5('<datetime>|<raw-pos0>'),
        NO normalization. BookOrbit hashes the exact pos0 the bridge uploaded,
        so the key list the bridge sends it must use the raw pos0 too — the
        internal normalized identity would not match BookOrbit's side."""
        import hashlib
        raw = f"{datetime_str or ''}|{pos0 or ''}"
        return hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()

    @staticmethod
    def _annotation_entry_fields(entry: dict) -> dict:
        """Normalize an incoming annotation entry's content fields."""
        def _s(key, maxlen=None):
            value = entry.get(key)
            if value is None:
                return None
            value = str(value)
            return value[:maxlen] if maxlen else value

        pageno = entry.get("pageno")
        try:
            pageno = int(pageno) if pageno is not None else None
        except (TypeError, ValueError):
            pageno = None

        return {
            "datetime_updated": _s("datetimeUpdated", 19) or _s("datetime_updated", 19),
            "pos_format": (_s("posFormat", 16) or _s("pos_format", 16) or "xpointer"),
            "pos0": _s("pos0", 4000),
            "pos1": _s("pos1", 4000),
            "drawer": _s("drawer", 16),
            "color": _s("color", 30),
            "text": _s("text"),
            "note": _s("note"),
            "chapter": _s("chapter", 500),
            "pageno": pageno,
        }

    @staticmethod
    def _annotation_content_differs(row: KoreaderAnnotation, fields: dict) -> bool:
        for key in ("pos0", "pos1", "drawer", "color", "text", "note", "chapter", "pageno", "datetime_updated"):
            if getattr(row, key) != fields.get(key):
                return True
        return False

    @staticmethod
    def _doc_fragment_index(pos0) -> Optional[int]:
        match = re.search(r"/DocFragment\[(\d+)\]", str(pos0 or ""))
        return int(match.group(1)) if match else None

    def _match_spoke_row_by_content(self, session, user_id, doc_md5: str, fields: dict) -> Optional[KoreaderAnnotation]:
        """Fallback identity match for lossy-position spokes: a CFI round-trip
        never reproduces the device's xpointer serialization, so ann_key
        lookups miss. Match by the highlighted text within the same
        DocFragment instead — only when the match is unambiguous."""
        text = fields.get("text")
        if not text:
            return None
        candidates = (
            session.query(KoreaderAnnotation)
            .filter(
                KoreaderAnnotation.md5 == doc_md5,
                KoreaderAnnotation.user_id == user_id,
                KoreaderAnnotation.deleted == False,  # noqa: E712
                KoreaderAnnotation.text == text,
            )
            .all()
        )
        fragment = self._doc_fragment_index(fields.get("pos0"))
        if fragment is not None:
            candidates = [r for r in candidates if self._doc_fragment_index(r.pos0) == fragment]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _annotation_response_entry(row: KoreaderAnnotation) -> dict:
        return {
            "serverId": row.id,
            "version": row.version,
            "datetime": row.datetime,
            "datetimeUpdated": row.datetime_updated,
            "drawer": row.drawer,
            "color": row.color,
            "text": row.text,
            "note": row.note,
            "chapter": row.chapter,
            "pageno": row.pageno,
            "posFormat": row.pos_format,
            "pos0": row.pos0,
            "pos1": row.pos1,
        }

    @staticmethod
    def _get_device_state(session, annotation_id: int, device_key: str) -> Optional[KoreaderAnnotationDeviceState]:
        return (
            session.query(KoreaderAnnotationDeviceState)
            .filter(
                KoreaderAnnotationDeviceState.annotation_id == annotation_id,
                KoreaderAnnotationDeviceState.device_key == device_key,
            )
            .first()
        )

    def _set_device_state(self, session, annotation_id: int, device_key: str,
                          acked_version: int = None, ack_deleted: bool = None) -> None:
        state = self._get_device_state(session, annotation_id, device_key)
        if state is None:
            state = KoreaderAnnotationDeviceState(annotation_id=annotation_id, device_key=device_key)
            session.add(state)
        if acked_version is not None:
            state.acked_version = max(int(state.acked_version or 0), int(acked_version))
        if ack_deleted is not None:
            state.ack_deleted = bool(ack_deleted)
        state.updated_at = utcnow()

    def exchange_koreader_annotations(self, user_id, device_key: str, books: list[dict]) -> dict:
        """Two-way annotation exchange for one device (mirrors the BookOrbit protocol).

        Per book ``{hash, keys: [{k, dt}], keysComplete, changes: [entry...]}``:
        upserts the device's changed entries, tombstones entries the device
        deleted (key missing from a complete key list for an annotation this
        device previously had), then returns the per-device delta
        ``{hash, toApply: {add, edit, delete}}`` computed from ack state.
        """
        device_key = str(device_key or "").strip()
        if not device_key:
            return {"books": []}

        response_books = []
        with self.get_session() as session:
            for book in books or []:
                doc_md5 = str(book.get("hash") or "").strip().lower()
                if not doc_md5:
                    continue

                incoming_changes = book.get("changes") or []
                incoming_keys = book.get("keys") or []
                keys_complete = bool(book.get("keysComplete"))

                # 1. Upsert this device's changed entries.
                for entry in incoming_changes:
                    if not isinstance(entry, dict):
                        continue
                    dt = str(entry.get("datetime") or "").strip()
                    fields = self._annotation_entry_fields(entry)
                    if not dt or not fields["pos0"]:
                        continue
                    ann_key = self.compute_annotation_key(dt, fields["pos0"])
                    row = (
                        session.query(KoreaderAnnotation)
                        .filter(
                            KoreaderAnnotation.md5 == doc_md5,
                            KoreaderAnnotation.user_id == user_id,
                            KoreaderAnnotation.ann_key == ann_key,
                        )
                        .first()
                    )
                    if row is None:
                        row = KoreaderAnnotation(
                            md5=doc_md5, user_id=user_id, ann_key=ann_key,
                            datetime=dt, source_device=device_key, **fields,
                        )
                        session.add(row)
                        session.flush()
                        self._set_device_state(session, row.id, device_key, acked_version=row.version)
                        continue

                    if row.deleted:
                        state = self._get_device_state(session, row.id, device_key)
                        if state is not None and state.ack_deleted:
                            # The device saw the tombstone and re-created the
                            # highlight afterwards — resurrect it.
                            row.deleted = False
                            row.deleted_at = None
                            for key, value in fields.items():
                                setattr(row, key, value)
                            row.source_device = device_key
                            row.version = int(row.version or 1) + 1
                            row.updated_at = utcnow()
                            self._set_device_state(session, row.id, device_key,
                                                   acked_version=row.version, ack_deleted=False)
                        # else: another device deleted it and this device hasn't
                        # heard yet — the delete wins; its re-upload is stale.
                        continue

                    if self._annotation_content_differs(row, fields):
                        incoming_dt = fields.get("datetime_updated") or dt
                        current_dt = row.datetime_updated or row.datetime
                        if incoming_dt >= current_dt:
                            for key, value in fields.items():
                                setattr(row, key, value)
                            row.source_device = device_key
                            row.version = int(row.version or 1) + 1
                            row.updated_at = utcnow()
                    self._set_device_state(session, row.id, device_key, acked_version=row.version)

                # 2. Deletion detection: keys this device previously had but no
                # longer lists (only trustworthy when the key list is complete).
                if keys_complete:
                    present_keys = {
                        str(k.get("k") or "").strip().lower()
                        for k in incoming_keys if isinstance(k, dict)
                    }
                    present_keys.discard("")
                    candidates = (
                        session.query(KoreaderAnnotation)
                        .filter(
                            KoreaderAnnotation.md5 == doc_md5,
                            KoreaderAnnotation.user_id == user_id,
                            KoreaderAnnotation.deleted == False,  # noqa: E712
                        )
                        .all()
                    )
                    for row in candidates:
                        if row.ann_key in present_keys:
                            continue
                        state = self._get_device_state(session, row.id, device_key)
                        device_knew_it = (
                            row.source_device == device_key
                            or (state is not None and int(state.acked_version or 0) > 0)
                        )
                        if not device_knew_it:
                            continue
                        row.deleted = True
                        row.deleted_at = utcnow()
                        row.version = int(row.version or 1) + 1
                        row.updated_at = utcnow()
                        self._set_device_state(session, row.id, device_key,
                                               acked_version=row.version, ack_deleted=True)

                # 3. Per-device delta.
                delta = self._compute_annotation_delta(session, user_id, doc_md5, device_key)
                response_books.append({
                    "hash": doc_md5,
                    "toApply": {
                        "add": delta["add"],
                        "edit": delta["edit"],
                        "delete": delta["delete"],
                    },
                    "more": delta["more"],
                })

            session.commit()
        return {"books": response_books}

    def _compute_annotation_delta(self, session, user_id, doc_md5: str, device_key: str) -> dict:
        adds, edits, deletes = [], [], []
        pending_count = 0
        rows = (
            session.query(KoreaderAnnotation)
            .filter(
                KoreaderAnnotation.md5 == doc_md5,
                KoreaderAnnotation.user_id == user_id,
            )
            .order_by(KoreaderAnnotation.id)
            .all()
        )
        for row in rows:
            state = self._get_device_state(session, row.id, device_key)
            if row.deleted:
                if state is not None and int(state.acked_version or 0) > 0 and not state.ack_deleted:
                    pending_count += 1
                    if pending_count <= self._ANNOTATION_APPLY_CAP:
                        deletes.append({"serverId": row.id, "datetime": row.datetime})
                continue
            acked = int(state.acked_version or 0) if state is not None else 0
            if acked >= int(row.version or 1):
                continue
            pending_count += 1
            if pending_count > self._ANNOTATION_APPLY_CAP:
                continue
            entry = self._annotation_response_entry(row)
            if acked == 0:
                adds.append(entry)
            else:
                edits.append(entry)
        return {
            "add": adds,
            "edit": edits,
            "delete": deletes,
            "more": pending_count > self._ANNOTATION_APPLY_CAP,
        }

    def ack_koreader_annotations(self, user_id, device_key: str, books: list[dict]) -> dict:
        """Record which exchanged annotations a device actually applied/deleted.

        A 'failed' status is recorded like 'applied' so the entry is not re-sent
        forever (the device kept the text; it just couldn't anchor it)."""
        device_key = str(device_key or "").strip()
        if not device_key:
            return {"acked": 0}

        acked = 0
        with self.get_session() as session:
            for book in books or []:
                for item in (book.get("applied") or []):
                    try:
                        server_id = int(item.get("serverId"))
                        version = int(item.get("version") or 0)
                    except (TypeError, ValueError):
                        continue
                    row = session.query(KoreaderAnnotation).filter(
                        KoreaderAnnotation.id == server_id,
                        KoreaderAnnotation.user_id == user_id,
                    ).first()
                    if row is None:
                        continue
                    self._set_device_state(session, server_id, device_key,
                                           acked_version=version or row.version)
                    acked += 1
                for item in (book.get("deleted") or []):
                    try:
                        server_id = int(item.get("serverId"))
                    except (TypeError, ValueError):
                        continue
                    row = session.query(KoreaderAnnotation).filter(
                        KoreaderAnnotation.id == server_id,
                        KoreaderAnnotation.user_id == user_id,
                    ).first()
                    if row is None:
                        continue
                    self._set_device_state(session, server_id, device_key, ack_deleted=True)
                    acked += 1
            session.commit()
        return {"acked": acked}

    def get_annotation_md5s_for_user(self, user_id) -> list[str]:
        """Distinct document md5s that have annotations for a user (incl. tombstones)."""
        with self.get_session() as session:
            rows = (
                session.query(KoreaderAnnotation.md5)
                .filter(KoreaderAnnotation.user_id == user_id)
                .distinct()
                .all()
            )
            return [r[0] for r in rows]

    def get_annotation_spoke_state(self, user_id, doc_md5: str, spoke_key: str,
                                   server_id_field: str = "bookorbit_server_id",
                                   version_field: str = "bookorbit_version",
                                   exclude_if_set: str = None) -> dict:
        """Everything the spoke needs to build one exchange call for one book:
        alive keys, changed entries (version above the spoke's ack), and the
        spoke's pending tombstone acks.

        ``exclude_if_set`` omits rows with that field non-null from ``changes``
        (they still contribute keys) — used so rows owned by one Grimmory store
        are never exported into the other."""
        doc_md5 = str(doc_md5 or "").strip().lower()
        with self.get_session() as session:
            rows = (
                session.query(KoreaderAnnotation)
                .filter(
                    KoreaderAnnotation.md5 == doc_md5,
                    KoreaderAnnotation.user_id == user_id,
                )
                .all()
            )
            keys, changes, pending_delete_acks, pending_deletes = [], [], [], []
            for row in rows:
                state = self._get_device_state(session, row.id, spoke_key)
                acked = int(state.acked_version or 0) if state is not None else 0
                if row.deleted:
                    # Deletions propagate to the spoke by key omission; remember
                    # rows whose tombstone the spoke hasn't processed yet.
                    if not (state is not None and state.ack_deleted):
                        pending_delete_acks.append(row.id)
                        spoke_id = getattr(row, server_id_field, None)
                        if spoke_id is not None:
                            pending_deletes.append({"_id": row.id, "serverId": int(spoke_id)})
                    continue
                # BookOrbit hashes the raw pos0 it received; send its convention.
                keys.append({"k": self._bookorbit_annotation_key(row.datetime, row.pos0), "dt": row.datetime})
                if exclude_if_set is not None and getattr(row, exclude_if_set, None) is not None:
                    continue
                if acked < int(row.version or 1):
                    entry = self._annotation_response_entry(row)
                    entry["_id"] = row.id  # internal: for post-upload ack bookkeeping
                    entry["_spoke_server_id"] = getattr(row, server_id_field, None)
                    entry["_spoke_version"] = getattr(row, version_field, None)
                    changes.append(entry)
            return {
                "keys": keys,
                "changes": changes,
                "pending_delete_acks": pending_delete_acks,
                "pending_deletes": pending_deletes,
            }

    def apply_spoke_annotations(self, user_id, doc_md5: str, spoke_key: str,
                                adds: list[dict], edits: list[dict], deletes: list[dict],
                                server_id_field: str = "bookorbit_server_id",
                                version_field: str = "bookorbit_version",
                                synced_at_field: str = "bookorbit_synced_at",
                                trust_positions: bool = True) -> dict:
        """Apply a spoke's (e.g. BookOrbit's) toApply delta into the canonical store.

        ``trust_positions=False`` is for spokes whose positions are lossy
        projections (Grimmory converts xpointer<->CFI, so a pulled pos0 never
        matches the device's serialization byte-for-byte). For those, a pull
        must never rewrite a matched row's identity (datetime/pos0/pos1/
        ann_key) — rewriting the key makes the device's next complete key list
        look like a deletion and the row gets tombstoned everywhere. Only the
        spoke-editable content (note/color/drawer) is merged, unmatched rows
        fall back to a text-within-fragment match before creating anything,
        and tombstoned rows are never revived by a stale remote echo.

        Returns the ack payload data: applied [{serverId, version}] and deleted
        [{serverId}] to report back to the spoke."""
        doc_md5 = str(doc_md5 or "").strip().lower()
        applied_acks, deleted_acks = [], []
        with self.get_session() as session:
            for entry in list(adds or []) + list(edits or []):
                if not isinstance(entry, dict):
                    continue
                dt = str(entry.get("datetime") or "").strip()
                fields = self._annotation_entry_fields(entry)
                spoke_id = entry.get("serverId")
                spoke_version = entry.get("version")
                if not dt or not fields["pos0"] or spoke_id is None:
                    continue

                row = None
                if spoke_id is not None:
                    row = (
                        session.query(KoreaderAnnotation)
                        .filter(
                            KoreaderAnnotation.user_id == user_id,
                            getattr(KoreaderAnnotation, server_id_field) == int(spoke_id),
                        )
                        .first()
                    )
                ann_key = self.compute_annotation_key(dt, fields["pos0"])
                if row is None:
                    row = (
                        session.query(KoreaderAnnotation)
                        .filter(
                            KoreaderAnnotation.md5 == doc_md5,
                            KoreaderAnnotation.user_id == user_id,
                            KoreaderAnnotation.ann_key == ann_key,
                        )
                        .first()
                    )
                if row is None and not trust_positions:
                    row = self._match_spoke_row_by_content(session, user_id, doc_md5, fields)

                if row is not None and row.deleted and not trust_positions:
                    # Stale remote echo of a locally tombstoned annotation —
                    # never revive through a lossy spoke; the push loop deletes
                    # the remote copy instead.
                    continue

                if row is None:
                    row = KoreaderAnnotation(
                        md5=doc_md5, user_id=user_id, ann_key=ann_key,
                        datetime=dt, source_device=spoke_key, **fields,
                    )
                    setattr(row, server_id_field, int(spoke_id))
                    if spoke_version is not None:
                        setattr(row, version_field, int(spoke_version))
                    setattr(row, synced_at_field, utcnow())
                    session.add(row)
                    session.flush()
                    self._set_device_state(session, row.id, spoke_key, acked_version=row.version)
                else:
                    setattr(row, server_id_field, int(spoke_id))
                    if spoke_version is not None:
                        setattr(row, version_field, int(spoke_version))
                    setattr(row, synced_at_field, utcnow())
                    if row.deleted:
                        row.deleted = False
                        row.deleted_at = None
                    if trust_positions:
                        if self._annotation_content_differs(row, fields):
                            for key, value in fields.items():
                                setattr(row, key, value)
                            row.source_device = spoke_key
                            row.version = int(row.version or 1) + 1
                            row.updated_at = utcnow()
                        # The ann_key follows pos0 edits so device key lists
                        # stay consistent — anchored to the row's own datetime,
                        # never the entry's: devices hash (datetime|pos0), and a
                        # spoke timestamp (e.g. BookFusion's added_at, which
                        # moves on our own create/PATCH) in the key desyncs it
                        # from every device's key list.
                        row.ann_key = self.compute_annotation_key(row.datetime, row.pos0)
                    else:
                        # Identity (datetime/pos0/pos1/ann_key) stays canonical:
                        # the spoke's round-tripped position is a lossy
                        # projection, not an edit. Merge only what the spoke
                        # can legitimately change.
                        content_keys = ("drawer", "color", "note")
                        if any(getattr(row, key) != fields.get(key) for key in content_keys):
                            for key in content_keys:
                                setattr(row, key, fields.get(key))
                            row.datetime_updated = fields.get("datetime_updated") or row.datetime_updated
                            row.source_device = spoke_key
                            row.version = int(row.version or 1) + 1
                            row.updated_at = utcnow()
                    self._set_device_state(session, row.id, spoke_key, acked_version=row.version)
                applied_acks.append({
                    "serverId": int(spoke_id),
                    "version": int(spoke_version or 1),
                    "status": "applied",
                })

            for entry in deletes or []:
                spoke_id = entry.get("serverId") if isinstance(entry, dict) else None
                if spoke_id is None:
                    continue
                row = (
                    session.query(KoreaderAnnotation)
                    .filter(
                        KoreaderAnnotation.user_id == user_id,
                        getattr(KoreaderAnnotation, server_id_field) == int(spoke_id),
                    )
                    .first()
                )
                if row is not None and not row.deleted:
                    row.deleted = True
                    row.deleted_at = utcnow()
                    row.version = int(row.version or 1) + 1
                    row.updated_at = utcnow()
                    self._set_device_state(session, row.id, spoke_key,
                                           acked_version=row.version, ack_deleted=True)
                deleted_acks.append({"serverId": int(spoke_id), "status": "applied"})

            session.commit()
        return {"applied": applied_acks, "deleted": deleted_acks}

    def mark_spoke_annotations_uploaded(self, user_id, spoke_key: str,
                                        annotation_ids: list[int],
                                        tombstone_ids: list[int] = None,
                                        server_id_field: str = "bookorbit_server_id",
                                        version_field: str = "bookorbit_version",
                                        synced_at_field: str = "bookorbit_synced_at",
                                        server_ids_by_annotation_id: dict = None,
                                        versions_by_annotation_id: dict = None) -> None:
        """Record that the spoke accepted our uploaded changes / processed our
        key-omission deletions, so they are not re-sent every cycle."""
        with self.get_session() as session:
            for ann_id in annotation_ids or []:
                row = session.query(KoreaderAnnotation).filter(
                    KoreaderAnnotation.id == int(ann_id),
                    KoreaderAnnotation.user_id == user_id,
                ).first()
                if row is not None:
                    ann_key = str(ann_id)
                    server_ids = server_ids_by_annotation_id or {}
                    versions = versions_by_annotation_id or {}
                    if ann_key in server_ids and server_ids[ann_key] is not None:
                        setattr(row, server_id_field, int(server_ids[ann_key]))
                    elif ann_id in server_ids and server_ids[ann_id] is not None:
                        setattr(row, server_id_field, int(server_ids[ann_id]))
                    if ann_key in versions and versions[ann_key] is not None:
                        setattr(row, version_field, int(versions[ann_key]))
                    elif ann_id in versions and versions[ann_id] is not None:
                        setattr(row, version_field, int(versions[ann_id]))
                    self._set_device_state(session, row.id, spoke_key, acked_version=row.version)
                    setattr(row, synced_at_field, utcnow())
            for ann_id in tombstone_ids or []:
                row = session.query(KoreaderAnnotation).filter(
                    KoreaderAnnotation.id == int(ann_id),
                    KoreaderAnnotation.user_id == user_id,
                ).first()
                if row is not None:
                    self._set_device_state(session, row.id, spoke_key, ack_deleted=True)
            session.commit()

    def get_spoke_server_ids_for_book(self, user_id, doc_md5: str,
                                      server_id_field: str = "bookorbit_server_id") -> list[int]:
        """Known remote annotation ids for a spoke/book, excluding local tombstones."""
        doc_md5 = str(doc_md5 or "").strip().lower()
        with self.get_session() as session:
            rows = (
                session.query(getattr(KoreaderAnnotation, server_id_field))
                .filter(
                    KoreaderAnnotation.md5 == doc_md5,
                    KoreaderAnnotation.user_id == user_id,
                    KoreaderAnnotation.deleted == False,  # noqa: E712
                    getattr(KoreaderAnnotation, server_id_field).isnot(None),
                )
                .all()
            )
            return [int(r[0]) for r in rows if r[0] is not None]

    def get_unacked_annotation_versions(self, user_id, doc_md5: str, spoke_key: str) -> dict:
        """``{annotation_id: version}`` for alive rows whose current version the
        spoke has not acknowledged.

        Push-selection primitive for one-way spokes (Readest/Hardcover): row
        versions move only on content changes, so bookkeeping-column writes
        (which bump ``updated_at`` via onupdate) can never re-qualify a row —
        ``updated_at > <spoke>_synced_at`` comparisons self-invalidate and
        re-push everything every cycle."""
        doc_md5 = str(doc_md5 or "").strip().lower()
        with self.get_session() as session:
            rows = (
                session.query(KoreaderAnnotation)
                .filter(
                    KoreaderAnnotation.md5 == doc_md5,
                    KoreaderAnnotation.user_id == user_id,
                    KoreaderAnnotation.deleted == False,  # noqa: E712
                )
                .all()
            )
            unacked = {}
            for row in rows:
                state = self._get_device_state(session, row.id, spoke_key)
                acked = int(state.acked_version or 0) if state is not None else 0
                version = int(row.version or 1)
                if acked < version:
                    unacked[row.id] = version
            return unacked

    def get_unacked_annotation_tombstones(self, user_id, doc_md5: str, spoke_key: str) -> list[int]:
        """Ids of tombstoned rows the spoke has seen alive but not yet as deleted."""
        doc_md5 = str(doc_md5 or "").strip().lower()
        with self.get_session() as session:
            rows = (
                session.query(KoreaderAnnotation)
                .filter(
                    KoreaderAnnotation.md5 == doc_md5,
                    KoreaderAnnotation.user_id == user_id,
                    KoreaderAnnotation.deleted == True,  # noqa: E712
                )
                .all()
            )
            pending = []
            for row in rows:
                state = self._get_device_state(session, row.id, spoke_key)
                if state is not None and int(state.acked_version or 0) > 0 and not state.ack_deleted:
                    pending.append(row.id)
            return pending

    def ack_annotation_versions(self, user_id, spoke_key: str,
                                versions_by_id: dict = None,
                                deleted_ids: list = None) -> None:
        """Record a spoke's acks: content versions it pushed/absorbed and
        tombstones it processed, so neither is re-sent every cycle."""
        with self.get_session() as session:
            for ann_id, version in (versions_by_id or {}).items():
                row = session.query(KoreaderAnnotation).filter(
                    KoreaderAnnotation.id == int(ann_id),
                    KoreaderAnnotation.user_id == user_id,
                ).first()
                if row is not None:
                    self._set_device_state(session, row.id, spoke_key, acked_version=int(version))
            for ann_id in deleted_ids or []:
                row = session.query(KoreaderAnnotation).filter(
                    KoreaderAnnotation.id == int(ann_id),
                    KoreaderAnnotation.user_id == user_id,
                ).first()
                if row is not None:
                    self._set_device_state(session, row.id, spoke_key, ack_deleted=True)
            session.commit()

    def get_user_annotations_for_book(self, user_id, doc_md5: str, include_deleted: bool = False) -> list:
        """All annotation rows for a (user, document) — dashboard/tests helper."""
        doc_md5 = str(doc_md5 or "").strip().lower()
        with self.get_session() as session:
            query = session.query(KoreaderAnnotation).filter(
                KoreaderAnnotation.md5 == doc_md5,
                KoreaderAnnotation.user_id == user_id,
            )
            if not include_deleted:
                query = query.filter(KoreaderAnnotation.deleted == False)  # noqa: E712
            rows = query.order_by(KoreaderAnnotation.datetime).all()
            session.expunge_all()
            return rows
