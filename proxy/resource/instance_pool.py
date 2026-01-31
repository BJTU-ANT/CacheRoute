# proxy/resource/instance_pool.py
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class InstanceLoad:
    # 先预留，当前阶段不强依赖
    inflight: Optional[int] = None
    qps_1m: Optional[float] = None
    gpu_util: Optional[float] = None


@dataclass
class InstanceInfo:
    instance_id: str
    host: str
    port: int
    endpoints: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    weight: float = 1.0
    meta: Dict[str, Any] = field(default_factory=dict)

    load: InstanceLoad = field(default_factory=InstanceLoad)
    registered_at: int = field(default_factory=lambda: int(time.time()))
    last_seen_at: int = field(default_factory=lambda: int(time.time()))


class InstancePool:
    """
    In-memory instance pool (TTL based).
    - upsert: register/update instance static fields
    - heartbeat: refresh last_seen and optionally update load fields
    - list(include_dead=False): returns alive instances by default
    """
    def __init__(self, ttl_s: int = 30):
        self._ttl_s = int(ttl_s)
        self._lock = threading.Lock()
        self._items: Dict[str, InstanceInfo] = {}

    @property
    def ttl_s(self) -> int:
        return self._ttl_s

    def upsert(
        self,
        instance_id: str,
        host: str,
        port: int,
        endpoints: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        weight: float = 1.0,
        meta: Optional[Dict[str, Any]] = None,
    ) -> InstanceInfo:
        now = int(time.time())
        with self._lock:
            if instance_id in self._items:
                it = self._items[instance_id]
                it.host = host
                it.port = int(port)
                it.endpoints = endpoints or it.endpoints
                it.tags = tags or it.tags
                it.weight = float(weight)
                if meta:
                    it.meta.update(meta)
                it.last_seen_at = now
                return it

            it = InstanceInfo(
                instance_id=instance_id,
                host=host,
                port=int(port),
                endpoints=endpoints or [],
                tags=tags or [],
                weight=float(weight),
                meta=meta or {},
                registered_at=now,
                last_seen_at=now,
            )
            self._items[instance_id] = it
            return it

    def heartbeat(
        self,
        instance_id: str,
        inflight: Optional[int] = None,
        qps_1m: Optional[float] = None,
        gpu_util: Optional[float] = None,
    ) -> bool:
        now = int(time.time())
        with self._lock:
            it = self._items.get(instance_id)
            if not it:
                return False
            it.last_seen_at = now
            # 只在传入时更新，避免覆盖为 0/None
            if inflight is not None:
                it.load.inflight = int(inflight)
            if qps_1m is not None:
                it.load.qps_1m = float(qps_1m)
            if gpu_util is not None:
                it.load.gpu_util = float(gpu_util)
            return True

    def remove(self, instance_id: str) -> bool:
        with self._lock:
            return self._items.pop(instance_id, None) is not None

    def list(self, include_dead: bool = False) -> List[InstanceInfo]:
        now = int(time.time())
        with self._lock:
            items = list(self._items.values())

        if include_dead:
            return items

        alive: List[InstanceInfo] = []
        for it in items:
            if (now - int(it.last_seen_at)) <= self._ttl_s:
                alive.append(it)
        return alive
