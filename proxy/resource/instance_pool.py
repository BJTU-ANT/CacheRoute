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

    def report_resource_snapshot(self, instance_id: str, snapshot: Dict[str, Any]) -> bool:
        now = int(time.time())
        with self._lock:
            it = self._items.get(instance_id)
            if not it:
                return False
            it.last_seen_at = now
            it.resource = _resource_from_snapshot(snapshot=snapshot, reported_at=now)
            return True

    def build_pool_resource_snapshot(
        self,
        proxy_id: str,
        capacity: int = 0,
        prepare_queue_depth: Optional[int] = None,
        ready_queue_depth: Optional[int] = None,
    ) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            items = list(self._items.values())

        total = len(items)
        alive_items = [it for it in items if (now - int(it.last_seen_at)) <= self._ttl_s]
        alive = len(alive_items)
        reporting = [it for it in alive_items if it.resource.resource_reported_at is not None]
        missing_resource = max(0, alive - len(reporting))

        freshness = [now - float(it.resource.resource_reported_at or now) for it in reporting]
        admission_counts = {"accepting": 0, "degraded": 0, "rejecting": 0}
        for it in reporting:
            state = (it.resource.admission_state or "").strip().lower()
            if state in admission_counts:
                admission_counts[state] += 1

        inflight_total = sum(int(it.load.inflight or 0) for it in alive_items)
        qps_1m_total = sum(float(it.load.qps_1m or 0.0) for it in alive_items)
        capacity = int(capacity or 0)

        cpu_values = [float(it.resource.cpu_util) for it in reporting if it.resource.cpu_util is not None]
        gpu_values = [float(it.resource.gpu_util_avg) for it in reporting if it.resource.gpu_util_avg is not None]
        mem_used = sum(float(it.resource.memory_used_mb or 0.0) for it in reporting)
        mem_total = sum(float(it.resource.memory_total_mb or 0.0) for it in reporting)
        mem_free_ratios = [float(it.resource.memory_free_ratio) for it in reporting if it.resource.memory_free_ratio is not None]
        gpu_mem_used = sum(float(it.resource.gpu_mem_used_mb or 0.0) for it in reporting)
        gpu_mem_total = sum(float(it.resource.gpu_mem_total_mb or 0.0) for it in reporting)
        net_rx = sum(float(it.resource.network_rx_mbps or 0.0) for it in reporting)
        net_tx = sum(float(it.resource.network_tx_mbps or 0.0) for it in reporting)

        if alive == 0:
            pool_state = "rejecting"
        elif reporting and admission_counts["rejecting"] == len(reporting) and missing_resource == 0:
            pool_state = "rejecting"
        elif admission_counts["degraded"] > 0 or admission_counts["rejecting"] > 0 or missing_resource > 0:
            pool_state = "degraded"
        else:
            pool_state = "accepting"

        return {
            "schema_version": 1,
            "proxy_id": proxy_id,
            "generated_at": now,
            "ttl_s": self._ttl_s,
            "resource_freshness_s": {
                "min": min(freshness) if freshness else None,
                "avg": (sum(freshness) / len(freshness)) if freshness else None,
                "max": max(freshness) if freshness else None,
            },
            "instances": {
                "total": total,
                "alive": alive,
                "stale": max(0, total - alive),
                "with_resource": len(reporting),
                "missing_resource": missing_resource,
                "accepting": admission_counts["accepting"],
                "degraded": admission_counts["degraded"],
                "rejecting": admission_counts["rejecting"],
            },
            "load": {
                "inflight_total": inflight_total,
                "qps_1m_total": qps_1m_total,
                "load_ratio": float(inflight_total) / float(max(capacity, 1)),
                "capacity": capacity,
                "prepare_queue_depth": prepare_queue_depth,
                "ready_queue_depth": ready_queue_depth,
            },
            "utilization": {
                "cpu_avg": (sum(cpu_values) / len(cpu_values)) if cpu_values else None,
                "cpu_max": max(cpu_values) if cpu_values else None,
                "memory_used_mb": mem_used if reporting else None,
                "memory_total_mb": mem_total if reporting else None,
                "memory_used_ratio": (mem_used / mem_total) if mem_total > 0 else None,
                "memory_free_ratio_min": min(mem_free_ratios) if mem_free_ratios else None,
                "gpu_util_avg": (sum(gpu_values) / len(gpu_values)) if gpu_values else None,
                "gpu_util_max": max(gpu_values) if gpu_values else None,
                "gpu_mem_used_mb": gpu_mem_used if reporting else None,
                "gpu_mem_total_mb": gpu_mem_total if reporting else None,
                "gpu_mem_used_ratio": (gpu_mem_used / gpu_mem_total) if gpu_mem_total > 0 else None,
                "network_rx_mbps_total": net_rx if reporting else None,
                "network_tx_mbps_total": net_tx if reporting else None,
            },
            "pool_admission_state": pool_state,
            "pool_admission_reason": (
                f"{admission_counts['accepting']} accepting, "
                f"{admission_counts['degraded']} degraded, "
                f"{admission_counts['rejecting']} rejecting, "
                f"{alive} alive"
            ),
        }

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


def _resource_from_snapshot(snapshot: Dict[str, Any], reported_at: int) -> InstanceResource:
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
        raw_resource=dict(snapshot) if isinstance(snapshot, dict) else {},
    )
