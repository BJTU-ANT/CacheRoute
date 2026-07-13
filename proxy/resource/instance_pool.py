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
class InstanceResource:
    cpu_util: Optional[float] = None
    memory_used_mb: Optional[float] = None
    memory_total_mb: Optional[float] = None
    memory_free_mb: Optional[float] = None
    memory_free_ratio: Optional[float] = None
    gpu_util_avg: Optional[float] = None
    gpu_mem_used_mb: Optional[float] = None
    gpu_mem_total_mb: Optional[float] = None
    network_rx_mbps: Optional[float] = None
    network_tx_mbps: Optional[float] = None
    admission_state: Optional[str] = None
    resource_ts_ms: Optional[int] = None
    resource_reported_at: Optional[int] = None
    resource_report_monotonic_ms: Optional[int] = None
    resource_report_wall_time_ms: Optional[int] = None
    reported_instance_id: Optional[str] = None
    raw_resource: Dict[str, Any] = field(default_factory=dict)


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
    resource: InstanceResource = field(default_factory=InstanceResource)
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

    def report_resource_snapshot(
        self,
        instance_id: str,
        snapshot: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        now = int(time.time())
        with self._lock:
            it = self._items.get(instance_id)
            if not it:
                return False
            it.last_seen_at = now
            it.resource = _resource_from_snapshot(snapshot=snapshot, reported_at=now, metadata=metadata or {})
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


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _resource_from_snapshot(snapshot: Dict[str, Any], reported_at: int, metadata: Dict[str, Any]) -> InstanceResource:
    devices = snapshot.get("devices") if isinstance(snapshot, dict) else {}
    devices = devices if isinstance(devices, dict) else {}
    cpu = devices.get("cpu") if isinstance(devices.get("cpu"), dict) else {}
    memory = devices.get("memory") if isinstance(devices.get("memory"), dict) else {}
    capacity = snapshot.get("capacity_hint") if isinstance(snapshot.get("capacity_hint"), dict) else {}

    gpus = devices.get("gpu") if isinstance(devices.get("gpu"), list) else []
    gpu_utils: List[float] = []
    gpu_mem_used = 0.0
    gpu_mem_total = 0.0
    for gpu in gpus:
        if not isinstance(gpu, dict):
            continue
        util = _as_float(gpu.get("utilization_pct"))
        if util is not None:
            gpu_utils.append(util)
        gpu_mem_used += _as_float(gpu.get("memory_used_mb")) or 0.0
        gpu_mem_total += _as_float(gpu.get("memory_total_mb")) or 0.0

    networks = devices.get("network") if isinstance(devices.get("network"), list) else []
    first_net = networks[0] if networks and isinstance(networks[0], dict) else {}

    return InstanceResource(
        cpu_util=_as_float(cpu.get("utilization_pct")),
        memory_used_mb=_as_float(memory.get("used_mb")),
        memory_total_mb=_as_float(memory.get("total_mb")),
        memory_free_mb=_as_float(memory.get("free_mb")),
        memory_free_ratio=_as_float(capacity.get("memory_free_ratio")),
        gpu_util_avg=(sum(gpu_utils) / len(gpu_utils)) if gpu_utils else None,
        gpu_mem_used_mb=gpu_mem_used if gpus else None,
        gpu_mem_total_mb=gpu_mem_total if gpus else None,
        network_rx_mbps=_as_float(first_net.get("rx_mbps")),
        network_tx_mbps=_as_float(first_net.get("tx_mbps")),
        admission_state=str(capacity.get("admission_state")) if capacity.get("admission_state") is not None else None,
        resource_ts_ms=_as_int(snapshot.get("timestamp_ms")),
        resource_reported_at=reported_at,
        resource_report_monotonic_ms=_as_int(metadata.get("report_monotonic_ms")),
        resource_report_wall_time_ms=_as_int(metadata.get("report_wall_time_ms")),
        reported_instance_id=str(metadata.get("reported_instance_id")) if metadata.get("reported_instance_id") is not None else None,
        raw_resource=dict(snapshot) if isinstance(snapshot, dict) else {},
    )
