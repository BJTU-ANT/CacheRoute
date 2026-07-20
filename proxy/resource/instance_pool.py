# proxy/resource/instance_pool.py
"""Maintains proxy-side registered instance state and load information."""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from proxy.strategy.least_load import LeastLoadStrategy


@dataclass
class InstanceLoad:
    # Reserved first; not strongly required at this stage
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
                if it.load.inflight is None:
                    it.load.inflight = 0
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
                load=InstanceLoad(inflight=0),
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
            # Inflight is Proxy-maintained via begin_request/end_request.
            # Keep heartbeat load updates for non-lifecycle signals only.
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


    def begin_request(self, instance_id: str) -> bool:
        """Increment the Proxy-maintained inflight counter for an Instance."""
        with self._lock:
            it = self._items.get(instance_id)
            if not it:
                return False
            current = int(it.load.inflight or 0)
            it.load.inflight = current + 1
            return True

    def end_request(self, instance_id: str) -> bool:
        """Decrement the Proxy-maintained inflight counter without going below zero."""
        with self._lock:
            it = self._items.get(instance_id)
            if not it:
                return False
            current = int(it.load.inflight or 0)
            it.load.inflight = max(0, current - 1)
            return True

    def snapshot_instance_loads(
        self,
        queue_depths: Optional[Dict[str, Any]] = None,
        include_dead: bool = True,
    ) -> Dict[str, Any]:
        """Return per-Instance local load counters and optional queue-depth hints."""
        now = int(time.time())
        per_instance_queue = {}
        if queue_depths:
            raw = queue_depths.get("per_instance", {})
            if isinstance(raw, dict):
                per_instance_queue = raw

        with self._lock:
            items = list(self._items.values())

        score_strategy = LeastLoadStrategy()
        score_hint = {"queue_depths": queue_depths} if queue_depths is not None else None

        instances: List[Dict[str, Any]] = []
        for it in items:
            is_alive = (now - int(it.last_seen_at)) <= self._ttl_s
            if not include_dead and not is_alive:
                continue
            queue_item = per_instance_queue.get(it.instance_id, {})
            inflight = it.load.inflight
            qps_1m = it.load.qps_1m
            least_load_score = score_strategy.compute_score(it, hint=score_hint)
            instances.append({
                "instance_id": it.instance_id,
                "is_alive": is_alive,
                "inflight": inflight,
                "qps_1m": qps_1m,
                "prepare_queue_depth": queue_item.get("prepare_queue_depth"),
                "ready_queue_depth": queue_item.get("ready_queue_depth"),
                "active_prepare": queue_item.get("active_prepare"),
                "active_ready": queue_item.get("active_ready"),
                "least_load_score": least_load_score,
            })

        return {
            "ttl_s": self._ttl_s,
            "generated_at": time.time(),
            "metric_source": {
                "inflight": "proxy_lifecycle_counter",
                "qps_1m": "instance_heartbeat",
                "queue_depth": "proxy_queue_manager" if queue_depths is not None else "unavailable",
            },
            "instances": instances,
        }

    def build_pool_resource_snapshot(
        self,
        proxy_id: str,
        capacity: int = 0,
        prepare_queue_depth: Optional[int] = None,
        ready_queue_depth: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Build a compact Proxy pool-level resource snapshot for Scheduler reporting.

        Null means unavailable/not wired; zero means the metric is actually reported as zero.
        This keeps Scheduler-side consumers from mistaking placeholders for measured load.
        """
        now = time.time()
        with self._lock:
            items = list(self._items.values())

        alive_items: List[InstanceInfo] = []
        stale = 0
        for it in items:
            if (now - float(it.last_seen_at)) <= self._ttl_s:
                alive_items.append(it)
            else:
                stale += 1

        reporting = [it for it in alive_items if _has_resource(it.resource)]
        freshness = [max(0.0, now - float(it.resource.resource_reported_at or now)) for it in reporting]

        admission_counts = {"accepting": 0, "degraded": 0, "rejecting": 0}
        for it in reporting:
            state = (it.resource.admission_state or "").strip().lower()
            if state in admission_counts:
                admission_counts[state] += 1

        missing_resource = max(0, len(alive_items) - len(reporting))
        inflight_values = [int(it.load.inflight) for it in alive_items if it.load.inflight is not None]
        qps_values = [float(it.load.qps_1m) for it in alive_items if it.load.qps_1m is not None]
        inflight_total = sum(inflight_values) if inflight_values else None
        qps_1m_total = sum(qps_values) if qps_values else None
        effective_capacity = int(capacity or 0)
        load_ratio = _ratio(float(inflight_total), float(effective_capacity)) if inflight_total is not None else None
        queued_total = None
        if prepare_queue_depth is not None or ready_queue_depth is not None:
            queued_total = int(prepare_queue_depth or 0) + int(ready_queue_depth or 0)
        queue_pressure = _ratio(float(queued_total), float(effective_capacity)) if queued_total is not None else None

        cpu_values = [float(it.resource.cpu_util) for it in reporting if it.resource.cpu_util is not None]
        gpu_values = [float(it.resource.gpu_util_avg) for it in reporting if it.resource.gpu_util_avg is not None]
        mem_used_values = [float(it.resource.memory_used_mb) for it in reporting if it.resource.memory_used_mb is not None]
        mem_total_values = [float(it.resource.memory_total_mb) for it in reporting if it.resource.memory_total_mb is not None]
        gpu_mem_used_values = [float(it.resource.gpu_mem_used_mb) for it in reporting if it.resource.gpu_mem_used_mb is not None]
        gpu_mem_total_values = [float(it.resource.gpu_mem_total_mb) for it in reporting if it.resource.gpu_mem_total_mb is not None]
        mem_used = sum(mem_used_values) if mem_used_values else None
        mem_total = sum(mem_total_values) if mem_total_values else None
        gpu_mem_used = sum(gpu_mem_used_values) if gpu_mem_used_values else None
        gpu_mem_total = sum(gpu_mem_total_values) if gpu_mem_total_values else None
        mem_free_ratios = [float(it.resource.memory_free_ratio) for it in reporting if it.resource.memory_free_ratio is not None]
        rx_values = [float(it.resource.network_rx_mbps) for it in reporting if it.resource.network_rx_mbps is not None]
        tx_values = [float(it.resource.network_tx_mbps) for it in reporting if it.resource.network_tx_mbps is not None]

        if len(alive_items) == 0:
            pool_state = "rejecting"
        elif reporting and admission_counts["rejecting"] == len(reporting) and missing_resource == 0:
            pool_state = "rejecting"
        elif admission_counts["degraded"] or admission_counts["rejecting"] or missing_resource:
            pool_state = "degraded"
        else:
            pool_state = "accepting"

        metric_source = {
            "instances": "proxy_instance_pool_ttl",
            "resource": "instance_resource_snapshot" if reporting else "unavailable",
            "inflight_total": "proxy_lifecycle_counter" if inflight_values else "unavailable",
            "qps_1m_total": "instance_heartbeat" if qps_values else "unavailable",
            "load_ratio": "derived_from_inflight_capacity" if load_ratio is not None else "unavailable",
            "capacity": "proxy_config",
            "prepare_queue_depth": "proxy_queue_manager" if prepare_queue_depth is not None else "unavailable",
            "ready_queue_depth": "proxy_queue_manager" if ready_queue_depth is not None else "unavailable",
            "queue_pressure": "derived_from_queue_depth_capacity" if queue_pressure is not None else "unavailable",
            "utilization": "instance_resource_snapshot" if reporting else "unavailable",
            "pool_admission_state": "derived_from_instance_resource_admission",
            "resource_freshness_s": "derived_from_instance_resource_report_time" if reporting else "unavailable",
        }
        metric_quality = {
            "resource": _quality(len(reporting), len(alive_items)),
            "load": _quality(len(inflight_values) + len(qps_values), max(1, len(alive_items) * 2)) if alive_items else "missing",
            "queue": "complete" if prepare_queue_depth is not None and ready_queue_depth is not None else "missing",
        }

        return {
            "schema_version": 1,
            "proxy_id": proxy_id,
            "generated_at": now,
            "ttl_s": self._ttl_s,
            "metric_source": metric_source,
            "metric_quality": metric_quality,
            "resource_freshness_s": _min_avg_max(freshness),
            "instances": {
                "total": len(items),
                "alive": len(alive_items),
                "stale": stale,
                "with_resource": len(reporting),
                "missing_resource": missing_resource,
                "accepting": admission_counts["accepting"],
                "degraded": admission_counts["degraded"],
                "rejecting": admission_counts["rejecting"],
            },
            "load": {
                "inflight_total": inflight_total,
                "qps_1m_total": qps_1m_total,
                "load_ratio": load_ratio,
                "capacity": effective_capacity,
                "prepare_queue_depth": prepare_queue_depth,
                "ready_queue_depth": ready_queue_depth,
                "queue_pressure": queue_pressure,
            },
            "utilization": {
                "cpu_avg": _avg(cpu_values),
                "cpu_max": max(cpu_values) if cpu_values else None,
                "memory_used_mb": mem_used,
                "memory_total_mb": mem_total,
                "memory_used_ratio": _ratio(mem_used, mem_total),
                "memory_free_ratio_min": min(mem_free_ratios) if mem_free_ratios else None,
                "gpu_util_avg": _avg(gpu_values),
                "gpu_util_max": max(gpu_values) if gpu_values else None,
                "gpu_mem_used_mb": gpu_mem_used,
                "gpu_mem_total_mb": gpu_mem_total,
                "gpu_mem_used_ratio": _ratio(gpu_mem_used, gpu_mem_total),
                "network_rx_mbps_total": sum(rx_values) if rx_values else None,
                "network_tx_mbps_total": sum(tx_values) if tx_values else None,
            },
            "pool_admission_state": pool_state,
            "pool_admission_reason": (
                f"{admission_counts['accepting']} accepting, {admission_counts['degraded']} degraded, "
                f"{admission_counts['rejecting']} rejecting, {len(alive_items)} alive"
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


def _has_resource(resource: InstanceResource) -> bool:
    return bool(resource.raw_resource) and resource.resource_reported_at is not None


def _avg(values: List[float]) -> Optional[float]:
    return (sum(values) / len(values)) if values else None


def _min_avg_max(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"min": None, "avg": None, "max": None}
    return {"min": min(values), "avg": sum(values) / len(values), "max": max(values)}


def _ratio(used: Optional[float], total: Optional[float]) -> Optional[float]:
    return (used / total) if used is not None and total is not None and total > 0 else None


def _quality(populated: int, expected: int) -> str:
    if expected <= 0 or populated <= 0:
        return "missing"
    if populated >= expected:
        return "complete"
    return "partial"


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
