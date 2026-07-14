# kdn_server/text_db.py
"""SQLite-backed text knowledge database with embedding and KVCache status metadata."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import numpy as np
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _normalize_text(s: str) -> str:
    # Stable and reproducible: normalize newlines and trim only; do not fold case or spaces to avoid accidental merges.
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s.strip()


def compute_kid(content: str) -> str:
    norm = _normalize_text(content)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KBItem:
    id: str
    rel_path: str
    content: str
    length: int
    embedding: Optional[List[float]] = None
    embed_dim: Optional[int] = None
    kv_ready: int = 0
    kv_rel_dir: Optional[str] = None
    kv_dumped_keys: Optional[int] = None
    kv_updated_at: Optional[int] = None


class TextDatabase:
    def __init__(self, base_dir: str, embedder=None):
        self._embedder = embedder
        self.base_dir = Path(base_dir).resolve()
        self.blocks_dir = self.base_dir / "blocks"
        self.db_path = self.base_dir / "index.sqlite3"
        self.blocks_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._ensure_kv_columns()
        self._ensure_embedding_columns()

    def _connect(self) -> sqlite3.Connection:
        # Use one independent connection per operation for concurrency and threads; uvicorn defaults to async plus thread pool.
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL improves concurrent read/write behavior; writes remain serialized, but reads are not blocked by long writes.
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_blocks (
                    kid TEXT PRIMARY KEY,
                    rel_path TEXT NOT NULL,
                    length INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    meta_json TEXT,
                    embedding BLOB,
                    embed_dim INTEGER
                );
                """
            )

    def _ensure_kv_columns(self) -> None:
        with self._connect() as conn:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(knowledge_blocks);").fetchall()]
            if "kv_ready" not in cols:
                conn.execute("ALTER TABLE knowledge_blocks ADD COLUMN kv_ready INTEGER DEFAULT 0;")
            if "kv_rel_dir" not in cols:
                conn.execute("ALTER TABLE knowledge_blocks ADD COLUMN kv_rel_dir TEXT;")
            if "kv_dumped_keys" not in cols:
                conn.execute("ALTER TABLE knowledge_blocks ADD COLUMN kv_dumped_keys INTEGER;")
            if "kv_updated_at" not in cols:
                conn.execute("ALTER TABLE knowledge_blocks ADD COLUMN kv_updated_at INTEGER;")

    def _ensure_embedding_columns(self) -> None:
        with self._connect() as conn:
            cols = [r["name"] for r in conn.execute(
                "PRAGMA table_info(knowledge_blocks);"
            ).fetchall()]

            if "embedding" not in cols:
                conn.execute("ALTER TABLE knowledge_blocks ADD COLUMN embedding BLOB;")
            if "embed_dim" not in cols:
                conn.execute("ALTER TABLE knowledge_blocks ADD COLUMN embed_dim INTEGER;")

    def register_text(self, content: str, meta: Optional[Dict[str, Any]] = None) -> Tuple[str, str, int]:
        """
        Return: (kid, status, length)
          status: "created" | "exists"
        """
        if not isinstance(content, str):
            raise TypeError("content must be str")

        norm = _normalize_text(content)
        if not norm:
            raise ValueError("content is empty after normalization")

        kid = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        rel_path = f"blocks/{kid}.txt"
        final_path = self.base_dir / rel_path
        length = len(norm)
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        embedding_blob = None
        embed_dim = None
        if self._embedder is not None:
            vec = self._embedder.encode_vector(norm)[0]
            vec = np.asarray(vec, dtype=np.float32)
            embedding_blob = vec.tobytes()
            embed_dim = int(vec.shape[0])

        # Check the index first; return directly on hit for idempotency.
        with self._connect() as conn:
            row = conn.execute("SELECT kid FROM knowledge_blocks WHERE kid = ?", (kid,)).fetchone()
            if row:
                return kid, "exists", length

        # Atomic file write: tmp -> replace.
        tmp_dir = self.base_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"{kid}.{os.getpid()}.tmp"
        tmp_path.write_text(norm, encoding="utf-8")
        os.replace(tmp_path, final_path)

        # Write index in a transaction to keep index and file consistent; INSERT OR IGNORE preserves idempotency under concurrent registration of the same kid.
        now = int(time.time())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                """
                INSERT OR IGNORE INTO knowledge_blocks
                (kid, rel_path, length, created_at, meta_json, embedding, embed_dim)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (kid, rel_path, int(length), now, meta_json, embedding_blob, embed_dim),
            )
            conn.execute("COMMIT;")

        return kid, "created", length

    def mark_kv_ready(self, kid: str, kv_rel_dir: str, dumped_keys: int, updated_at: Optional[int] = None) -> None:
        kid = (kid or "").strip().lower()
        if not kid:
            raise ValueError("empty kid")

        ts = int(time.time()) if updated_at is None else int(updated_at)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            # kid must already exist after text registration; otherwise decide whether KV-only without text is allowed.
            row = conn.execute("SELECT kid FROM knowledge_blocks WHERE kid=?", (kid,)).fetchone()
            if not row:
                conn.execute("ROLLBACK;")
                raise KeyError(f"kid not found in text index: {kid}")

            conn.execute(
                """
                UPDATE knowledge_blocks
                SET kv_ready=1, kv_rel_dir=?, kv_dumped_keys=?, kv_updated_at=?
                WHERE kid=?
                """,
                (kv_rel_dir, int(dumped_keys), ts, kid),
            )
            conn.execute("COMMIT;")

    def get_many(self, kids: Iterable[str]) -> Tuple[List[KBItem], List[str]]:
        items: List[KBItem] = []
        miss: List[str] = []

        kids_list = list(kids)
        if not kids_list:
            return items, miss

        # Batch index lookup, including embedding and embed_dim.
        q_marks = ",".join(["?"] * len(kids_list))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT kid, rel_path, length, embedding, embed_dim, kv_ready, kv_rel_dir, kv_dumped_keys, kv_updated_at
                FROM knowledge_blocks
                WHERE kid IN ({q_marks})
                """,
                tuple(kids_list),
            ).fetchall()

        # kid -> (rel_path, length, embedding_list_or_None, embed_dim_or_None)
        by_kid: Dict[str, Tuple[str, int, Optional[List[float]], Optional[int], int, Optional[str], Optional[int], Optional[int]]] = {}

        for r in rows:
            rel_path = r["rel_path"]
            length = int(r["length"])

            emb_list: Optional[List[float]] = None
            emb_dim: Optional[int] = None

            # Deserialize embedding: BLOB(float32 bytes) -> List[float].
            blob = r["embedding"]
            if blob is not None:
                emb_dim = int(r["embed_dim"]) if r["embed_dim"] is not None else None
                # Assumes registration wrote float32 values.
                arr = np.frombuffer(blob, dtype=np.float32)
                emb_list = arr.tolist()
                # Optional consistency check: validate dimension when embed_dim exists.
                if emb_dim is not None and emb_dim != len(emb_list):
                    # On dimension mismatch, options include raising, ignoring embedding, or marking miss.
                    # This path ignores the embedding to avoid one dirty row breaking the entire query.
                    emb_list = None

            kv_ready = int(r["kv_ready"]) if r["kv_ready"] is not None else 0
            kv_rel_dir = r["kv_rel_dir"]
            kv_dumped_keys = int(r["kv_dumped_keys"]) if r["kv_dumped_keys"] is not None else None
            kv_updated_at = int(r["kv_updated_at"]) if r["kv_updated_at"] is not None else None

            by_kid[r["kid"]] = (rel_path, length, emb_list, emb_dim, kv_ready, kv_rel_dir, kv_dumped_keys, kv_updated_at)

        # Return in input order without deduplication.
        for kid in kids_list:
            rec = by_kid.get(kid)
            if not rec:
                miss.append(kid)
                continue

            rel_path, length, emb_list, emb_dim, kv_ready, kv_rel_dir, kv_dumped_keys, kv_updated_at = rec
            p = (self.base_dir / rel_path).resolve()
            if not p.exists():
                miss.append(kid)
                continue

            content = p.read_text(encoding="utf-8")
            items.append(
                KBItem(
                    id=kid,
                    rel_path=rel_path,
                    content=content,
                    length=length,
                    embedding=emb_list,
                    embed_dim=emb_dim,
                    kv_ready=kv_ready,
                    kv_rel_dir=kv_rel_dir,
                    kv_dumped_keys=kv_dumped_keys,
                    kv_updated_at=kv_updated_at,
                )
            )

        return items, miss

    def snapshot(self, limit: int = 1000000, offset: int = 0, include_embedding: bool = True) -> List[dict]:
        """
        Return a knowledge-index snapshot without reading txt bodies, dedicated to scheduler initialization.
        - When include_embedding=True, deserialize embedding BLOBs into list[float].
        """
        limit = max(1, int(limit))
        offset = max(0, int(offset))

        cols = [
            "kid", "rel_path", "length",
            "embed_dim", "kv_ready", "kv_rel_dir",
            "kv_dumped_keys", "kv_updated_at",
        ]
        if include_embedding:
            cols.insert(3, "embedding")

        sql = f"""
        SELECT {",".join(cols)}
        FROM knowledge_blocks
        ORDER BY created_at ASC
        LIMIT ? OFFSET ?
        """

        with self._connect() as conn:
            rows = conn.execute(sql, (limit, offset)).fetchall()

        out: List[dict] = []
        for r in rows:
            it = dict(r)

            # embedding: BLOB -> list[float] (float32).
            if include_embedding:
                blob = it.get("embedding")
                if blob is not None:
                    arr = np.frombuffer(blob, dtype=np.float32)
                    it["embedding"] = arr.tolist()
                else:
                    it["embedding"] = None

            # Normalize types.
            it["length"] = int(it.get("length") or 0)
            it["embed_dim"] = int(it.get("embed_dim") or 0) if it.get("embed_dim") is not None else None
            it["kv_ready"] = int(it.get("kv_ready") or 0)
            it["kv_dumped_keys"] = int(it.get("kv_dumped_keys") or 0) if it.get("kv_dumped_keys") is not None else None
            it["kv_updated_at"] = int(it.get("kv_updated_at") or 0) if it.get("kv_updated_at") is not None else None

            out.append(it)

        return out


    def delete_one(self, kid: str) -> Tuple[bool, str]:
        """
        Delete one knowledge block, including index and file.
        Return (deleted, reason).
          deleted=True: index record was actually deleted, and file deletion was best-effort.
          deleted=False: not found or invalid argument.
        """
        kid = (kid or "").strip().lower()
        if not kid:
            return False, "empty kid"

        # Query the path first; return not_found directly if the index does not exist.
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rel_path FROM knowledge_blocks WHERE kid = ?",
                (kid,),
            ).fetchone()
            if not row:
                return False, "not_found"

            rel_path = row["rel_path"]
            file_path = (self.base_dir / rel_path).resolve()

            # Delete the index first inside a transaction, then delete the file to avoid rollback issues after deleting the wrong file.
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("DELETE FROM knowledge_blocks WHERE kid = ?", (kid,))
            conn.execute("COMMIT;")

        # File deletion is best-effort; missing files count as success because the index is already deleted.
        try:
            if file_path.exists():
                os.remove(file_path)
        except Exception as e:
            # Do not roll back the index here; return the exception as reason to help diagnose permission or lock issues.
            return True, f"index_deleted_file_delete_failed: {e}"

        return True, "deleted"

    def delete_many(self, kids: Iterable[str]) -> dict:
        """
        Delete in batches and return statistics.
        """
        deleted: List[str] = []
        not_found: List[str] = []
        errors: List[dict] = []

        for kid in kids:
            ok, reason = self.delete_one(kid)
            if ok:
                if reason == "deleted":
                    deleted.append(str(kid))
                else:
                    # For example, index_deleted_file_delete_failed.
                    deleted.append(str(kid))
                    errors.append({"kid": str(kid), "error": reason})
            else:
                not_found.append(str(kid))

        return {"deleted": deleted, "not_found": not_found, "errors": errors}
