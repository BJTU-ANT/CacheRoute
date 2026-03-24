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
from scheduler.resource.control_plane import _hb_agg

# kid -> signature
# signature = (embed_dim, length, kv_ready, kv_updated_at, kv_dumped_keys)
_kdn_meta_cache: Dict[str, Tuple] = {}


def _format_kdn_addr(host: str, port: int) -> str:
    return f"kdn://{host}:{int(port)}"


async def refresh_kdn_knowledge_index(app) -> Dict[str, Dict[str, Dict[str, int]]]:
    """
    为 scheduler 构建一个轻量级 per-KDN 知识元数据索引。

    返回结构：
      {
        <kdn_id>: {
          <kid>: {"length": 123, "kv_ready": 1},
          ...
        },
        "kdn://host:port": {...}  # 兼容地址查询
      }

    说明：
    - 这里只拉 metadata，不拉 embedding，避免把知识表构建逻辑和 KDN 选择逻辑混在一起。
    - 先顺序实现，保持最小改动；后续若 KDN 数量增多，可再并发化。
    """
    pool = getattr(app.state, "kdn_pool", None)
    if pool is None:
        app.state.kdn_knowledge_index = {}
        return {}

    alive = await pool.list(include_dead=False)
    if not alive:
        app.state.kdn_knowledge_index = {}
        return {}

    index: Dict[str, Dict[str, Dict[str, int]]] = {}
    for info in alive:
        base_url = f"http://{info.host}:{int(info.port)}"
        items = await fetch_kdn_snapshot(
            base_url=base_url,
            need_fields=["kid", "length", "kv_ready"],
        )

        meta_by_kid: Dict[str, Dict[str, int]] = {}
        for it in items:
            kid = str(it.get("kid") or "").strip().lower()
            if not kid:
                continue
            meta_by_kid[kid] = {
                "length": int(it.get("length") or 0),
                "kv_ready": int(it.get("kv_ready") or 0),
            }

        index[str(info.kdn_id)] = meta_by_kid
        index[_format_kdn_addr(info.host, info.port)] = meta_by_kid

    app.state.kdn_knowledge_index = index
    return index


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
    app.state.kdn_alive = len(alive)
    app.state.kdn_alive_addrs = alive_addrs  # ["kdn://h:p", ...]
    app.state.kdn_last_selected = base_url  # "http://h:p"
    app.state.kdn_last_selected_id = getattr(chosen, "kdn_id", "")

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
    Atomic-swap design:
      - NEVER mutate old_table in-place
      - Build / apply changes on new_table
      - Rebuild FAISS on new_table
      - Swap app.state.knowledge_table at the end
    """
    lock = getattr(app.state, "_kdn_refresh_lock", None)
    if lock is None:
        raise RuntimeError("scheduler.state._kdn_refresh_lock is not initialized")

    async with lock:
        kdn_knowledge_index = await refresh_kdn_knowledge_index(app)

        kdn_base_url, alive_addrs = await select_kdn_base_url(app)
        if not kdn_base_url:
            app.state.kdn_last_refresh_ok = False
            app.state.kdn_last_refresh_reason = "no alive kdn in pool"
            app.state.kdn_last_refresh_ts = int(time.time())
            return {"ok": False, "reason": "no alive kdn in pool"}

        # A) meta snapshot
        meta_items = await fetch_kdn_snapshot(
            base_url=kdn_base_url,
            need_fields=["kid", "embed_dim", "length",
                         "kv_ready", "kv_updated_at", "kv_dumped_keys"],
        )

        added, updated, removed = diff_kdn_meta(meta_items)

        old_table = getattr(app.state, "knowledge_table", None)

        # ---- Build new_table (either first build or clone from old) ----
        if old_table is None:
            embedder = getattr(app.state, "embedding_engine", None)
            if embedder is None:
                raise RuntimeError("embedding_engine is not initialized; cannot build KnowledgeTable")
            new_table = KnowledgeTable(dim=embedder.dim)
            logger.debug("[Scheduler] KnowledgeTable initialized (first build), dim=%s", embedder.dim)
        else:
            new_table = old_table.clone_without_index()

        changed = False

        # B) upsert on new_table
        need_fetch = added + updated
        if need_fetch:
            full_items = await fetch_kdn_items_by_kids(kdn_base_url, need_fetch)

            for it in full_items:
                kid_raw = it.get("kid") or it.get("id")  # compatibility
                kid = str(kid_raw or "").strip().lower()
                if not kid:
                    continue

                emb = it.get("embedding")
                if emb is None:
                    continue

                unit = KnowledgeUnit(
                    embedding=list(emb),
                    length=int(it.get("length") or 0),
                    # 当前 KnowledgeTable 继续保留“哪些 KDN 可用”的全局视角；
                    # 精确到每个 KDN 的覆盖信息由 kdn_knowledge_index 维护，供 cacheroute 使用。
                    avail_kdn_servers=list(alive_addrs),
                    kv_ready=int(it.get("kv_ready") or 0),
                    kv_rel_dir=it.get("kv_rel_dir"),
                    kv_dumped_keys=it.get("kv_dumped_keys"),
                    kv_updated_at=it.get("kv_updated_at"),
                )
                new_table.upsert_kid(kid, unit)
                changed = True

        # C) delete on new_table
        if removed:
            new_table.delete_kids(removed)
            changed = True

        # D) build FAISS on new_table (always build for first build; otherwise only if changed)
        if old_table is None or changed:
            new_table.build_faiss_index()

        # E) atomic swap
        app.state.knowledge_table = new_table

        # status
        app.state.last_refresh_ts = int(time.time())
        app.state.kdn_last_refresh_ok = True
        app.state.kdn_last_refresh_reason = ""
        app.state.kdn_last_refresh_ts = int(time.time())

        return {
            "ok": True,
            "added": len(added),
            "updated": len(updated),
            "removed": len(removed),
            "entries": len(getattr(new_table, "_units", {})),
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
            try:
                await _hb_agg.record_kdn_refresh(r)
            except Exception:
                # 输出层故障不能影响业务
                logger.debug("[Scheduler] record_kdn_refresh failed", exc_info=True)

            # 不再刷屏：降为 debug
            if r.get("ok"):
                logger.debug("[Scheduler] KDN auto-refresh OK: entries=%s, added=%s, updated=%s, removed=%s",
                             r.get("entries"), r.get("added"), r.get("updated"), r.get("removed"))
            else:
                logger.debug("[Scheduler] KDN auto-refresh skipped: %s", r)
        except Exception as e:
            logger.exception(f"[Scheduler] KDN auto-refresh FAILED: {e}")
            app.state.kdn_last_refresh_ok = False
            app.state.kdn_last_refresh_reason = f"exception: {type(e).__name__}"
            app.state.kdn_last_refresh_ts = int(time.time())

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

