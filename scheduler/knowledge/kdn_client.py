# kdn_client.py
"""
KDN Client utilities (used by Scheduler startup)
- snap Knowledge_list from KDN server during scheduler startup
- KDN interaction may grow (snapshot, delta updates, kv strategy, etc.).

Env:
- SCHEDULER_KDN_BASE_URL: e.g. "http://127.0.0.1:9101"
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional
import httpx


DEFAULT_TIMEOUT_S = 10.0


async def fetch_kdn_snapshot(
    base_url: str,
    need_fields: Optional[List[str]] = None,
    limit: int = 1_000_000,
    offset: int = 0,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> List[Dict[str, Any]]:
    """
    Pull knowledge snapshot from KDN:
      POST {base_url}/knowledge/snapshot
      body: {"need_fields":[...], "limit":..., "offset":...}

    Returns:
      list of items (each item contains at least kid, embedding, length, embed_dim)
    """
    base_url = base_url.rstrip("/")
    url = f"{base_url}/knowledge/snapshot"

    if not need_fields:
        need_fields = ["kid", "length", "embed_dim", "embedding", "kv_ready"]

    payload = {
        "need_fields": need_fields,
        "limit": int(limit),
        "offset": int(offset),
    }

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()

    items = data.get("items") or []
    if not isinstance(items, list):
        raise RuntimeError(f"KDN snapshot invalid response: items is not a list, got {type(items)}")

    return items


async def fetch_kdn_items_by_kids(
    base_url: str,
    kids: list[str],
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    """
    Fetch full items (with embedding) for specific kids.
    """
    base_url = base_url.rstrip("/")
    url = f"{base_url}/knowledge/search/text"

    payload = {
        "knowledge_ids": kids,
        "need_fields": ["kid", "length", "embed_dim", "embedding",
                        "kv_ready", "kv_rel_dir", "kv_dumped_keys", "kv_updated_at"],
    }

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()

    return data.get("items") or []
