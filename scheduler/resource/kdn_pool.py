# scheduler/resource/kdn_pool.py
"""Maintains Scheduler-side KDN resource state and load information."""
from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class KDNLoad:
    # Basic load
    items: int = 0
    qps_1m: float = 0.0
    # v0.1.7: KDN network/injection-side load used for CacheRoute overload checks
    pending_transfers: int = 0
    active_transfers: int = 0
    network_queue_ms_ema: float = 0.0


@dataclass
class KDNInfo:
    kdn_id: str
    host: str
    port: int
    endpoints: List[str] = field(default_factory=lambda: ["knowledge/snapshot", "knowledge/search/text"])
    tags: List[str] = field(default_factory=list)
    weight: float = 1.0
    meta: Dict[str, Any] = field(default_factory=dict)
    load: KDNLoad = field(default_factory=KDNLoad)

    registered_at: float = field(default_factory=lambda: time.time())
    last_seen_at: float = field(default_factory=lambda: time.time())

    def is_alive(self, ttl_s: int) -> bool:
        return (time.time() - float(self.last_seen_at)) <= float(ttl_s)


class KDNPool:
    """
    Scheduler-side pool for KDN servers.
    - register/upsert: idempotent
    - heartbeat: refresh last_seen
    - remove: delete
    - list: alive-only or all
    """
    def __init__(self, ttl_s: int = 30) -> None:
        self.ttl_s = int(ttl_s)
        self._lock = asyncio.Lock()
        self._data: Dict[str, KDNInfo] = {}

    async def upsert(self, info: KDNInfo) -> None:
        async with self._lock:
            now = time.time()
            old = self._data.get(info.kdn_id)
            if old is None:
                info.registered_at = now
            else:
                info.registered_at = old.registered_at
            info.last_seen_at = now
            self._data[info.kdn_id] = info

    async def heartbeat(self, kdn_id: str, load: Optional[KDNLoad] = None) -> bool:
        async with self._lock:
            it = self._data.get(kdn_id)
            if it is None:
                return False
            it.last_seen_at = time.time()
            if load is not None:
                it.load = load
            return True

    async def remove(self, kdn_id: str) -> None:
        async with self._lock:
            self._data.pop(kdn_id, None)

    async def list(self, include_dead: bool = False) -> List[KDNInfo]:
        async with self._lock:
            items = list(self._data.values())
        if include_dead:
            return items
        return [x for x in items if x.is_alive(self.ttl_s)]
