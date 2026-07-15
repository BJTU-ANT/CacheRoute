# scheduler/resource/proxy_pool.py
# -*- coding: utf-8 -*-
"""
Proxy resource pool: the state-model layer of the Scheduler control plane.

Design goals:
- control_plane.py only handles HTTP entry, parameter validation, and writing state into the pool
- Scheduling strategies (future CacheRoute policies) only read this pool to choose proxies
- Decouple the state model from the HTTP/protocol layer to prevent later strategy changes from affecting everything

The current implementation is in-memory (single process, single worker), which is enough for validation.
If distributed mode is needed later (multiple scheduler instances sharing proxy state), this file can be replaced with
Redis/etcd/SQL or similar storage without changing the control_plane API or scheduling code.
"""

from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ProxyLoad:
    """
    Proxy load-information structure, extensible later.

    Notes:
    - Avoid inventing too many load fields prematurely; start with the most general placeholder fields.
    - If proxies can periodically report statistics later (such as inflight/qps/gpu_util/kv_hit),
      only add fields here and update them in the control_plane heartbeat.
    """
    # ---- static capability (register-time) ----
    max_capacity: int = 0      # maximum processing capacity, reported at registration and unchanged during the lifecycle

    instance_count: int = 0     # number of instances managed by the proxy, reported at registration
    kv_mem_per_instance_gb: float = 0.0  # per-instance KV memory size in GB, reported at registration
    kv_cache_pool_gb: float = 0.0  # instance_count * kv_mem_per_instance_gb, computed by the scheduler

    # ---- dynamic load (heartbeat) ----
    inflight: int = 0          # number of requests currently being processed, or decode sessions
    qps_1m: float = 0.0        # QPS over the last minute, reported by the proxy
    gpu_util: float = 0.0      # GPU utilization, either 0-100 or 0-1 depending on the chosen convention


@dataclass
class ProxyInfo:
    """
    Proxy static + dynamic information structure.

    Static information, supplied at registration:
    - proxy_id/host/port/endpoints/tags/weight/meta

    Dynamic information, updated by heartbeat/monitoring:
    - load/last_seen_at

    Notes:
    - endpoints use OpenAI-style path fragments, for example ["chat/completions", "completions"]
    - meta stores extension fields that are inconvenient to structure, such as version, machine type, TP size, etc.
    """
    proxy_id: str
    host: str
    port: int
    endpoints: List[str] = field(default_factory=list)

    tags: List[str] = field(default_factory=list)
    weight: float = 1.0
    meta: Dict[str, Any] = field(default_factory=dict)
    kv_cache_update_policy: str = "lru"

    # Load information used by future scheduling strategies
    load: ProxyLoad = field(default_factory=ProxyLoad)

    # Timestamps: registration time and last heartbeat time
    registered_at: float = field(default_factory=lambda: time.time())
    last_seen_at: float = field(default_factory=lambda: time.time())
    pool_resource: Optional[Dict[str, Any]] = None
    pool_resource_reported_at: Optional[float] = None

    def touch(self) -> None:
        """Refresh last_seen_at when a heartbeat/update is received."""
        self.last_seen_at = time.time()

    def is_alive(self, ttl_s: int, now: Optional[float] = None) -> bool:
        """
        Determine whether the proxy is alive based on TTL.
        - ttl_s: mark inactive after no heartbeat for ttl_s
        - now: optional, used to reduce repeated time.time() calls during batch checks
        """
        now = now or time.time()
        return (now - self.last_seen_at) <= ttl_s


class ProxyPool:
    """
    Proxy resource pool (in-memory version).

    Concurrency model:
    - control_plane register/heartbeat/unregister can access it concurrently
    - the scheduler data plane (scheduling logic) can concurrently list/get
    - all reads and writes are serialized through asyncio.Lock to avoid state races

    Note:
    - This is a single-process in-memory structure. With multiple workers, each process gets its own pool.
      The current design (7002 embedded server) also requires a single process and single worker to avoid port conflicts.
    """
    def __init__(self, ttl_s: int = 30):
        self.ttl_s = ttl_s
        self._lock = asyncio.Lock()
        self._data: Dict[str, ProxyInfo] = {}

    async def upsert(self, info: ProxyInfo) -> None:
        """
        Register/update a proxy with idempotent upsert semantics.

        Behavior contract:
        - if proxy_id appears for the first time, insert directly
        - if the same proxy_id already exists, update all fields but keep the original registered_at as the first registration time
        """
        async with self._lock:
            old = self._data.get(info.proxy_id)
            if old is None:
                self._data[info.proxy_id] = info
                return

            # Keep the first registration time; use the newest values for other fields
            info.registered_at = old.registered_at
            if info.pool_resource is None:
                info.pool_resource = old.pool_resource
                info.pool_resource_reported_at = old.pool_resource_reported_at
            self._data[info.proxy_id] = info

    async def heartbeat(
        self,
        proxy_id: str,
        load: Optional[ProxyLoad] = None,
        meta_patch: Optional[Dict[str, Any]] = None,
        pool_resource: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Proxy heartbeat.
        - Success: refresh last_seen_at and also update load if provided
        - Failure: proxy_id does not exist, return False
        """
        async with self._lock:
            p = self._data.get(proxy_id)
            if not p:
                return False

            p.touch()
            if load is not None:
                p.load.inflight = int(load.inflight)
                p.load.qps_1m = float(load.qps_1m)
                p.load.gpu_util = float(load.gpu_util)
            if meta_patch:
                p.meta.update(dict(meta_patch))
            if pool_resource is not None:
                p.pool_resource = dict(pool_resource)
                p.pool_resource_reported_at = time.time()
            return True

    async def remove(self, proxy_id: str) -> None:
        """Unregister a proxy; do not error if it does not exist."""
        async with self._lock:
            self._data.pop(proxy_id, None)

    async def get(self, proxy_id: str) -> Optional[ProxyInfo]:
        """Get one proxy info object, possibly returning None."""
        async with self._lock:
            return self._data.get(proxy_id)

    async def list(self, include_dead: bool = False) -> List[ProxyInfo]:
        """
        List proxies.
        - include_dead=False: return only live proxies
        - include_dead=True：return all proxies, including inactive ones
        """
        async with self._lock:
            now = time.time()
            out: List[ProxyInfo] = []
            for p in self._data.values():
                alive = p.is_alive(self.ttl_s, now=now)
                if (not include_dead) and (not alive):
                    continue
                out.append(p)

            # Stable output order: sort by most recent heartbeat first
            out.sort(key=lambda x: x.last_seen_at, reverse=True)
            return out

    async def inflight_delta(self, proxy_id: str, delta: int) -> bool:
        async with self._lock:
            p = self._data.get(proxy_id)
            if not p:
                return False
            v = int(p.load.inflight) + int(delta)
            if v < 0:
                v = 0
            p.load.inflight = v
            return True
