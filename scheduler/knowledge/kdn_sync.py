# integration/kdn_sync.py
"""
KDN <-> Scheduler synchronization logic.

This module can be reused by:
- scheduler Knowledge list startup
- Knowledge list periodic refresh
- future admin-triggered refresh
"""
import logging
logger = logging.getLogger(__name__)

import asyncio
import time
import os

from typing import List, Dict, Tuple
from core.config import SCHEDULER_KDN_REFRESH_INTERVAL_S_DEFAULT
from store.knowledge_base import KnowledgeTable, KnowledgeUnit
from .kdn_client import fetch_kdn_snapshot, fetch_kdn_items_by_kids


# kid -> signature
# signature = (embed_dim, length, kv_ready, kv_updated_at, kv_dumped_keys)
_kdn_meta_cache: Dict[str, Tuple] = {}


def _format_kdn_addr(host: str, port: int) -> str:
    return f"kdn://{host}:{int(port)}"


async def select_kdn_base_url(app) -> tuple[str | None, list[str]]:
    """
    Select one alive KDN server from scheduler's kdn_pool.
    Returns:
      - base_url: "http://host:port" or None
      - alive_addrs: ["kdn://host:port", ...] (for KnowledgeUnit.avail_kdn_servers)
    """
    pool = getattr(app.state, "kdn_pool", None)
    if pool is None:
        return None, []

    alive = await pool.list(include_dead=False)
    if not alive:
        return None, []

    # Round-robin index stored on app.state (best-effort)
    idx = int(getattr(app.state, "_kdn_rr_idx", 0) or 0)
    chosen = alive[idx % len(alive)]
    app.state._kdn_rr_idx = idx + 1

    base_url = f"http://{chosen.host}:{int(chosen.port)}"
    alive_addrs = [_format_kdn_addr(x.host, x.port) for x in alive]
    return base_url, alive_addrs


def build_table_from_kdn_items(
    items: List[dict],
    dim: int,
    avail_kdn_servers: List[str],
) -> KnowledgeTable:
    """
    Build a KnowledgeTable from KDN snapshot items.

    Design:
    - kid(str) is the external primary key
    - FAISS internal id is hidden inside KnowledgeTable
    - kv metadata is preserved for scheduling decisions
    """
    table = KnowledgeTable(dim=dim)

    loaded = 0
    skipped_no_emb = 0

    for it in items:
        kid = str(it.get("kid") or "").strip().lower()
        emb = it.get("embedding")
        length = int(it.get("length") or 0)
        embed_dim = int(it.get("embed_dim") or dim)

        if not kid:
            continue
        if emb is None:
            skipped_no_emb += 1
            continue
        if embed_dim != dim:
            raise RuntimeError(f"Dim mismatch for kid={kid}: {embed_dim} != {dim}")

        unit = KnowledgeUnit(
            embedding=list(emb),
            length=length,
            avail_kdn_servers=list(avail_kdn_servers or []),
            text_abstract=None,

            # KV metadata
            kv_ready=int(it.get("kv_ready") or 0),
            kv_rel_dir=it.get("kv_rel_dir"),
            kv_dumped_keys=it.get("kv_dumped_keys"),
            kv_updated_at=it.get("kv_updated_at"),
        )

        table.upsert_kid(kid, unit)
        loaded += 1

    table.build_faiss_index()
    return table


async def kdn_refresh_once(app) -> dict:
    """
        Refresh knowledge_table once from KDN snapshot.
        Protected by a global lock to avoid concurrent refresh.
    """
    lock = getattr(app.state, "_kdn_refresh_lock", None)
    if lock is None:
        # 防御式：说明 scheduler 启动时没初始化 lock
        raise RuntimeError("scheduler.state._kdn_refresh_lock is not initialized")

    async with lock:
        kdn_base_url, alive_addrs = await select_kdn_base_url(app)
        if not kdn_base_url:
            return {"ok": False, "reason": "no alive kdn in pool"}

        # A) 拉轻量 meta snapshot
        meta_items = await fetch_kdn_snapshot(
            base_url=kdn_base_url,
            need_fields=["kid", "embed_dim", "length",
                         "kv_ready", "kv_updated_at", "kv_dumped_keys"],
        )

        added, updated, removed = diff_kdn_meta(meta_items)

        table = getattr(app.state, "knowledge_table", None)
        if table is None:
            # 首次构建：使用 scheduler embedder 的 dim（最可靠）
            embedder = getattr(app.state, "embedding_engine", None)
            if embedder is None:
                raise RuntimeError("embedding_engine is not initialized; cannot build KnowledgeTable")

            table = KnowledgeTable(dim=int(embedder.dim))
            app.state.knowledge_table = table
            logger.info("[Scheduler] KnowledgeTable initialized (first build), dim=%s", embedder.dim)

        changed = False

        # B) 新增 / 更新
        need_fetch = added + updated
        if need_fetch:
            full_items = await fetch_kdn_items_by_kids(kdn_base_url, need_fetch)

            for it in full_items:
                kid_raw = it.get("kid") or it.get("id")  # KDN /knowledge/search/text uses "id"
                kid = str(kid_raw or "").strip().lower()
                if not kid:
                    continue

                emb = it.get("embedding")
                if emb is None:
                    # 没 embedding 就跳过（不应发生，但要防御）
                    continue

                unit = KnowledgeUnit(
                    embedding=list(emb),
                    length=int(it.get("length") or 0),
                    avail_kdn_servers=list(alive_addrs),
                    kv_ready=int(it.get("kv_ready") or 0),
                    kv_rel_dir=it.get("kv_rel_dir"),
                    kv_dumped_keys=it.get("kv_dumped_keys"),
                    kv_updated_at=it.get("kv_updated_at"),
                )
                table.upsert_kid(kid, unit)
                changed = True

        # C) 删除
        if removed:
            table.delete_kids(removed)
            changed = True

        # D) 只在有变化时 rebuild FAISS
        if changed:
            table.build_faiss_index()

        app.state.last_refresh_ts = int(time.time())
        return {
            "ok": True,
            "added": len(added),
            "updated": len(updated),
            "removed": len(removed),
            "entries": len(getattr(table, "_units", {})),
        }


async def kdn_auto_refresh_loop(app, stop_event: asyncio.Event):
    """
    Periodically refresh scheduler knowledge_table from KDN.

    Properties:
    - non-blocking
    - failure-safe (keep old table)
    - atomic swap
    """
    interval_s = int(os.getenv("SCHEDULER_KDN_REFRESH_INTERVAL_S", str(SCHEDULER_KDN_REFRESH_INTERVAL_S_DEFAULT)))
    interval_s = max(5, interval_s)

    # kdn_base_url = getattr(app.state, "kdn_base_url", None)
    # if not kdn_base_url:
    #     return

    while not stop_event.is_set():
        try:
            r = await kdn_refresh_once(app)
            if r.get("ok"):
                logger.info(f"[Scheduler] KDN auto-refresh OK: entries={r['entries']}, added={r['added']}, updated={r['updated']}, removed={r['removed']}")
            else:
                logger.warning(f"[Scheduler] KDN auto-refresh skipped: {r}")
        except Exception as e:
            logger.exception(f"[Scheduler] KDN auto-refresh FAILED: {e}")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass


def diff_kdn_meta(items: list) -> tuple[list[str], list[str], list[str]]:
    """
    Return (added, updated, removed) kid lists.
    """
    global _kdn_meta_cache

    seen = set()
    added, updated = [], []

    for it in items:
        kid = str(it.get("kid") or "").strip().lower()
        if not kid:
            continue

        sig = (
            it.get("embed_dim"),
            it.get("length"),
            it.get("kv_ready"),
            it.get("kv_updated_at"),
            it.get("kv_dumped_keys"),
        )

        seen.add(kid)
        if kid not in _kdn_meta_cache:
            added.append(kid)
            _kdn_meta_cache[kid] = sig
        elif _kdn_meta_cache[kid] != sig:
            updated.append(kid)
            _kdn_meta_cache[kid] = sig

    removed = [k for k in _kdn_meta_cache.keys() if k not in seen]
    for k in removed:
        _kdn_meta_cache.pop(k, None)

    return added, updated, removed

