# proxy/resource/p_control_plane.py

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from .instance_pool import InstancePool

import logging
logger = logging.getLogger("proxy.p_control_plane")


_control_plane = FastAPI(title="CacheRoute Proxy Control Plane", version="v1")
_pool: Optional[InstancePool] = None
_kdn_links: Dict[str, Dict[str, Any]] = {}
_instance_kdn_links: Dict[str, Dict[str, Dict[str, Any]]] = {}
_kdn_links_lock = asyncio.Lock()
_resource_snapshot_seen: set[str] = set()
_unknown_resource_warn_at: Dict[str, float] = {}


def set_pool(pool: InstancePool) -> None:
    global _pool
    _pool = pool


def get_pool() -> InstancePool:
    if _pool is None:
        raise RuntimeError("InstancePool is not set. Call set_pool() in proxy startup.")
    return _pool


class InstanceRegisterReq(BaseModel):
    instance_id: Optional[str] = None
    host: str
    port: int
    endpoints: List[str] = []
    tags: List[str] = []
    weight: float = 1.0
    meta: Dict[str, Any] = {}


class InstanceHeartbeatReq(BaseModel):
    instance_id: str
    inflight: Optional[int] = None
    qps_1m: Optional[float] = None
    gpu_util: Optional[float] = None


class InstanceUnregisterReq(BaseModel):
    instance_id: str


class InstanceResourceSnapshotReq(BaseModel):
    instance_id: str
    snapshot: Dict[str, Any]
    metadata: Dict[str, Any] = {}


class TopologyReportReq(BaseModel):
    instance_id: str
    links: Dict[str, Dict[str, Any]]


async def get_kdn_links_snapshot() -> Dict[str, Dict[str, Any]]:
    async with _kdn_links_lock:
        return {k: dict(v) for k, v in _kdn_links.items()}


def _is_better_link(new_item: Dict[str, Any], old_item: Dict[str, Any]) -> bool:
    """
    比较两个 Instance->KDN 链路，返回 new_item 是否更优。
    规则：带宽更高优先；带宽相同时时延更低优先。
    """
    new_bw = float(new_item.get("bandwidth_mbps", 0.0) or 0.0)
    old_bw = float(old_item.get("bandwidth_mbps", 0.0) or 0.0)
    if new_bw != old_bw:
        return new_bw > old_bw
    new_lat = float(new_item.get("latency_ms", 1e9) or 1e9)
    old_lat = float(old_item.get("latency_ms", 1e9) or 1e9)
    return new_lat < old_lat


def _rebuild_best_kdn_links_locked() -> None:
    best: Dict[str, Dict[str, Any]] = {}
    for instance_id, links in _instance_kdn_links.items():
        for kdn_key, metrics in (links or {}).items():
            if not isinstance(metrics, dict):
                continue
            current = best.get(kdn_key)
            if current is None or _is_better_link(metrics, current):
                item = dict(metrics)
                item["reported_by"] = instance_id
                best[kdn_key] = item
    _kdn_links.clear()
    _kdn_links.update(best)


@_control_plane.get("/healthz")
async def healthz() -> Dict[str, Any]:
    pool = get_pool()
    return {"ok": True, "ttl_s": pool.ttl_s}




@_control_plane.get("/debug/status")
async def debug_status() -> Dict[str, Any]:
    pool = get_pool()
    alive_items = pool.list(include_dead=False)
    all_items = pool.list(include_dead=True)
    return {
        "ok": True,
        "ttl_s": pool.ttl_s,
        "alive_instances": len(alive_items),
        "total_instances": len(all_items),
        "expired_instances": max(0, len(all_items) - len(alive_items)),
        "sample_ids": [it.instance_id for it in alive_items[:10]],
        "topology_kdn_links": len(_kdn_links),
    }


@_control_plane.post("/v1/instance/register")
async def register(req: InstanceRegisterReq) -> Dict[str, Any]:
    pool = get_pool()
    instance_id = req.instance_id or f"hp_{req.host}:{req.port}"
    it = pool.upsert(
        instance_id=instance_id,
        host=req.host,
        port=req.port,
        endpoints=req.endpoints,
        tags=req.tags,
        weight=req.weight,
        meta=req.meta,
    )

    logger.info(
        "[ProxyCP] instance register: id=%s addr=%s:%s endpoints=%s tags=%s weight=%s meta=%s",
        it.instance_id, it.host, it.port, it.endpoints, it.tags, it.weight, it.meta
    )

    # 给 instance 建议心跳周期：固定 10s，或 ttl/3（取较小）
    hb = min(10, max(1, pool.ttl_s // 3))
    return {
        "instance_id": it.instance_id,
        "heartbeat_interval_s": hb,
        "ttl_s": pool.ttl_s,
    }


@_control_plane.post("/v1/instance/heartbeat")
async def heartbeat(req: InstanceHeartbeatReq) -> Dict[str, Any]:
    pool = get_pool()
    ok = pool.heartbeat(
        instance_id=req.instance_id,
        inflight=req.inflight,
        qps_1m=req.qps_1m,
        gpu_util=req.gpu_util,
    )
    if not ok:
        logger.warning("[ProxyCP] heartbeat for unknown instance_id=%s", req.instance_id)

    return {"ok": ok}


@_control_plane.post("/v1/instance/resource_snapshot")
async def report_resource_snapshot(req: InstanceResourceSnapshotReq) -> Dict[str, Any]:
    pool = get_pool()
    ok = pool.report_resource_snapshot(
        instance_id=req.instance_id,
        snapshot=req.snapshot,
        metadata=req.metadata,
    )
    if not ok:
        now = asyncio.get_running_loop().time()
        last = _unknown_resource_warn_at.get(req.instance_id, 0.0)
        if now - last >= 30.0:
            _unknown_resource_warn_at[req.instance_id] = now
            logger.warning("[ProxyCP] resource snapshot for unknown instance_id=%s", req.instance_id)
        return {"ok": False, "error": "unknown_instance"}
    if req.instance_id not in _resource_snapshot_seen:
        _resource_snapshot_seen.add(req.instance_id)
        logger.info("[ProxyCP] first resource snapshot updated: instance_id=%s", req.instance_id)
    else:
        logger.debug("[ProxyCP] resource snapshot updated: instance_id=%s", req.instance_id)
    return {"ok": True}


@_control_plane.post("/v1/instance/unregister")
async def unregister(req: InstanceUnregisterReq) -> Dict[str, Any]:
    pool = get_pool()
    ok = pool.remove(req.instance_id)
    async with _kdn_links_lock:
        _instance_kdn_links.pop(req.instance_id, None)
        _rebuild_best_kdn_links_locked()
    logger.info("[ProxyCP] instance unregister: id=%s ok=%s", req.instance_id, ok)
    return {"ok": ok}


@_control_plane.get("/v1/instance/list")
async def list_instances(include_dead: bool = False) -> List[Dict[str, Any]]:
    pool = get_pool()
    items = pool.list(include_dead=include_dead)
    out: List[Dict[str, Any]] = []
    now = int(time.time())
    for it in items:
        is_alive = (now - int(it.last_seen_at)) <= pool.ttl_s
        out.append({
            "instance_id": it.instance_id,
            "host": it.host,
            "port": it.port,
            "endpoints": it.endpoints,
            "tags": it.tags,
            "weight": it.weight,
            "meta": it.meta,
            "registered_at": it.registered_at,
            "last_seen_at": it.last_seen_at,
            "load": {
                "inflight": it.load.inflight,
                "qps_1m": it.load.qps_1m,
                "gpu_util": it.load.gpu_util,
            },
            "resource": {
                "cpu_util": it.resource.cpu_util,
                "memory_used_mb": it.resource.memory_used_mb,
                "memory_total_mb": it.resource.memory_total_mb,
                "memory_free_mb": it.resource.memory_free_mb,
                "memory_free_ratio": it.resource.memory_free_ratio,
                "gpu_util_avg": it.resource.gpu_util_avg,
                "gpu_mem_used_mb": it.resource.gpu_mem_used_mb,
                "gpu_mem_total_mb": it.resource.gpu_mem_total_mb,
                "network_rx_mbps": it.resource.network_rx_mbps,
                "network_tx_mbps": it.resource.network_tx_mbps,
                "admission_state": it.resource.admission_state,
                "resource_ts_ms": it.resource.resource_ts_ms,
                "resource_reported_at": it.resource.resource_reported_at,
                "resource_report_monotonic_ms": it.resource.resource_report_monotonic_ms,
                "resource_report_wall_time_ms": it.resource.resource_report_wall_time_ms,
                "reported_instance_id": it.resource.reported_instance_id,
                "raw_resource": it.resource.raw_resource,
            },
            # Always expose the real TTL-derived state so UIs can distinguish alive and stale rows.
            "is_alive": is_alive,
        })
    return out




@_control_plane.get("/debug/instance_resources")
async def debug_instance_resources(include_dead: bool = True) -> Dict[str, Any]:
    pool = get_pool()
    items = pool.list(include_dead=include_dead)
    resources: List[Dict[str, Any]] = []
    for it in items:
        resources.append({
            "instance_id": it.instance_id,
            "host": it.host,
            "port": it.port,
            "last_seen_at": it.last_seen_at,
            "resource": {
                "cpu_util": it.resource.cpu_util,
                "memory_used_mb": it.resource.memory_used_mb,
                "memory_total_mb": it.resource.memory_total_mb,
                "memory_free_mb": it.resource.memory_free_mb,
                "memory_free_ratio": it.resource.memory_free_ratio,
                "gpu_util_avg": it.resource.gpu_util_avg,
                "gpu_mem_used_mb": it.resource.gpu_mem_used_mb,
                "gpu_mem_total_mb": it.resource.gpu_mem_total_mb,
                "network_rx_mbps": it.resource.network_rx_mbps,
                "network_tx_mbps": it.resource.network_tx_mbps,
                "admission_state": it.resource.admission_state,
                "resource_ts_ms": it.resource.resource_ts_ms,
                "resource_reported_at": it.resource.resource_reported_at,
                "resource_report_monotonic_ms": it.resource.resource_report_monotonic_ms,
                "resource_report_wall_time_ms": it.resource.resource_report_wall_time_ms,
                "reported_instance_id": it.resource.reported_instance_id,
            },
        })
    return {"instances": resources}

@_control_plane.post("/v1/topology/report")
async def report_topology(req: TopologyReportReq) -> Dict[str, Any]:
    merged = 0
    async with _kdn_links_lock:
        sanitized: Dict[str, Dict[str, Any]] = {}
        for kdn_key, metrics in (req.links or {}).items():
            if not isinstance(metrics, dict):
                continue
            sanitized[str(kdn_key)] = dict(metrics)
        _instance_kdn_links[req.instance_id] = sanitized
        _rebuild_best_kdn_links_locked()
        merged = len(sanitized)
    logger.info("[ProxyCP] topology report: instance_id=%s links=%s", req.instance_id, merged)
    return {"ok": True, "merged_links": merged}


@_control_plane.get("/v1/topology/kdn_links")
async def list_topology_links() -> Dict[str, Any]:
    return {"kdn_links": await get_kdn_links_snapshot()}


# 对外导出 app
control_plane = _control_plane
