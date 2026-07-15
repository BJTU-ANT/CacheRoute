# scheduler/resource/control_plane.py
# -*- coding: utf-8 -*-
"""
Scheduler control plane:

Responsibilities:
- Provide external proxy register / heartbeat / unregister / query APIs
- Write state into ProxyPool (the resource pool)
- Contains no scheduling strategy and is not coupled to the 7001 data plane

Runtime model:
- Current architecture: when the 7001 scheduler starts, lifespan also starts an extra uvicorn Server listening on 7002
- The control_plane FastAPI app runs in the same process, so it naturally shares memory (the same ProxyPool instance)

Note:
- With multiple workers, each process would have its own ProxyPool and port 7002 would conflict.
  Therefore this design assumes a single-process, single-worker scheduler.
"""
from __future__ import annotations

import os
import time
# import time
import uuid
import asyncio

import logging
logger = logging.getLogger("scheduler.control_plane")
_hb_logger = logging.getLogger("scheduler.hbreport")

from typing import Any, Dict, List, Optional, Coroutine, Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .proxy_pool import ProxyInfo, ProxyLoad, ProxyPool
from .kdn_pool import KDNInfo, KDNLoad, KDNPool
from .hb_log import HeartbeatLogAggregator, hb_report_loop
from core import config

_pool: Optional[ProxyPool] = None
_kdn_pool: Optional[KDNPool] = None
_on_kdn_register: Optional[Callable[[], Coroutine[Any, Any, None]]] = None

CONTROL_PLANE_TTL_S = int(os.environ.get("SCHEDULER_PROXY_TTL_S", config.CONTROL_PLANE_TTL_S))
HEARTBEAT_INTERVAL_S = int(os.environ.get("SCHEDULER_PROXY_HEARTBEAT_S", config.HEARTBEAT_INTERVAL_S))

# ----------------------------
# Pydantic request/response models
# ----------------------------

class ProxyRegisterRequest(BaseModel):
    """
    Proxy registration request.

    Notes:
    - proxy_id is optional; the control plane generates one if it is omitted
    - host/port: the proxy address reachable by the Scheduler
    - endpoints: forwarding endpoints supported by the proxy (OpenAI-style path fragments)
    - tags/weight/meta: stored now so later scheduling strategies can use them
    """
    proxy_id: Optional[str] = Field(default=None)
    host: str
    port: int = Field(..., ge=1, le=65535)
    endpoints: List[str] = Field(default_factory=lambda: ["chat/completions", "completions"])
    tags: List[str] = Field(default_factory=list)
    weight: float = Field(default=1.0, ge=0.0)
    meta: Dict[str, Any] = Field(default_factory=dict)
    max_capacity: int = Field(default=0, ge=0)
    instance_count: int = Field(default=0, ge=0)
    kv_mem_per_instance_gb: float = Field(default=0.0, ge=0.0)
    kv_cache_update_policy: str = Field(default="lru")
    pool_resource: Optional[Dict[str, Any]] = None

class ProxyRegisterResponse(BaseModel):
    """
    Registration response:
    - proxy_id: unique ID confirmed by the control plane
    - heartbeat_interval_s: suggested heartbeat interval
    - ttl_s: inactive threshold
    """
    proxy_id: str
    heartbeat_interval_s: int
    ttl_s: int


class ProxyHeartbeatRequest(BaseModel):
    """
    Heartbeat request: currently only proxy_id is required
    Future extension: add load fields here to report inflight/gpu_util and similar metrics.
    """
    proxy_id: str

    # Future load reporting extension; enable it when ready
    inflight: Optional[int] = None
    qps_1m: Optional[float] = None
    gpu_util: Optional[float] = None
    meta_patch: Optional[Dict[str, Any]] = None
    pool_resource: Optional[Dict[str, Any]] = None


class ProxyUnregisterRequest(BaseModel):
    proxy_id: str


class ProxyInfoResponse(BaseModel):
    """
    Proxy information returned by external queries.
    The Pydantic model keeps the API stable and avoids exposing internal dataclass objects directly.
    """
    proxy_id: str
    host: str
    port: int
    endpoints: List[str]
    tags: List[str]
    weight: float
    meta: Dict[str, Any]

    max_capacity: int
    instance_count: int
    kv_mem_per_instance_gb: float
    kv_cache_pool_gb: float
    kv_cache_update_policy: str

    inflight: int
    qps_1m: float
    gpu_util: float

    registered_at: float
    last_seen_at: float
    is_alive: bool
    pool_resource: Optional[Dict[str, Any]] = None
    pool_resource_reported_at: Optional[float] = None


class KDNRegisterRequest(BaseModel):
    kdn_id: str
    host: str
    port: int = Field(..., ge=1, le=65535)
    endpoints: List[str] = Field(default_factory=lambda: ["knowledge/snapshot", "knowledge/search/text"])
    tags: List[str] = Field(default_factory=list)
    weight: float = Field(default=1.0, ge=0.0)
    meta: Dict[str, Any] = Field(default_factory=dict)

class KDNRegisterResponse(BaseModel):
    kdn_id: str
    heartbeat_interval_s: int
    ttl_s: int

class KDNHeartbeatRequest(BaseModel):
    kdn_id: str
    items: Optional[int] = None
    qps_1m: Optional[float] = None
    pending_transfers: Optional[int] = None
    active_transfers: Optional[int] = None
    network_queue_ms_ema: Optional[float] = None

class KDNUnregisterRequest(BaseModel):
    kdn_id: str

class KDNInfoResponse(BaseModel):
    kdn_id: str
    host: str
    port: int
    endpoints: List[str]
    tags: List[str]
    weight: float
    meta: Dict[str, Any]
    items: int
    qps_1m: float
    pending_transfers: int
    active_transfers: int
    network_queue_ms_ema: float
    registered_at: float
    last_seen_at: float
    is_alive: bool

# ----------------------------
# Externally callable pool methods
# ----------------------------
def set_pool(pool: ProxyPool) -> None:
    """The scheduler (7001) injects the shared ProxyPool instance at startup."""
    global _pool
    _pool = pool

def get_pool() -> ProxyPool:
    """Both the control plane and data plane use this to access the same pool."""
    if _pool is None:
        raise RuntimeError("ProxyPool is not initialized. Call set_pool() at scheduler startup.")
    return _pool

def set_kdn_pool(pool: KDNPool) -> None:
    global _kdn_pool
    _kdn_pool = pool

def get_kdn_pool() -> KDNPool:
    if _kdn_pool is None:
        raise RuntimeError("KDNPool is not initialized. Call set_kdn_pool() at scheduler startup.")
    return _kdn_pool

def set_on_kdn_register(cb: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Used to trigger KDN refresh updates."""
    global _on_kdn_register
    _on_kdn_register = cb

# ----------------------------
# FastAPI app + resource pool instances
# ----------------------------


control_plane = FastAPI(title="CacheRoute Scheduler Control Plane v1")

_HB_REPORT_INTERVAL_S = int(os.environ.get("SCHEDULER_HB_REPORT_INTERVAL_S", "30"))
_hb_agg = HeartbeatLogAggregator()

@control_plane.on_event("startup")
async def _hb_report_startup():
    async def _get_proxies() -> List[ProxyInfo]:
        # Read the current in-pool state, including load/last_seen
        return await get_pool().list(include_dead=True)

    async def _get_kdns() -> List[KDNInfo]:
        return await get_kdn_pool().list(include_dead=True)

    control_plane.state._hb_report_task = asyncio.create_task(
        hb_report_loop(
            _hb_agg,
            _hb_logger,
            interval_s=_HB_REPORT_INTERVAL_S,
            get_proxies=_get_proxies,
            get_kdns=_get_kdns,
        )
    )

@control_plane.on_event("shutdown")
async def _hb_report_shutdown():
    t = getattr(control_plane.state, "_hb_report_task", None)  # type: ignore
    if t is not None:
        t.cancel()
# Single-process shared resource pool: control-plane APIs write to it, and future scheduling strategies read from the same pool
# _pool = ProxyPool(ttl_s=CONTROL_PLANE_TTL_S)

# -------------------
# --- Proxy-side methods ---
# -------------------
def _to_response(info: ProxyInfo) -> ProxyInfoResponse:
    """Convert internal dataclasses to external response models."""
    pool = get_pool()
    return ProxyInfoResponse(
        proxy_id=info.proxy_id,
        host=info.host,
        port=info.port,
        endpoints=list(info.endpoints),
        tags=list(info.tags),
        weight=float(info.weight),
        meta=dict(info.meta),

        max_capacity=int(info.load.max_capacity),
        instance_count=int(info.load.instance_count),
        kv_mem_per_instance_gb=float(info.load.kv_mem_per_instance_gb),
        kv_cache_pool_gb=float(info.load.kv_cache_pool_gb),
        kv_cache_update_policy=str(info.kv_cache_update_policy),

        inflight=int(info.load.inflight),
        qps_1m=float(info.load.qps_1m),
        gpu_util=float(info.load.gpu_util),

        registered_at=float(info.registered_at),
        last_seen_at=float(info.last_seen_at),
        is_alive=info.is_alive(pool.ttl_s),
        pool_resource=dict(info.pool_resource) if info.pool_resource is not None else None,
        pool_resource_reported_at=info.pool_resource_reported_at,
    )


@control_plane.get("/healthz")
async def healthz():
    """
    Health check: verifies that 7002 started successfully and routes are reachable.
    """
    return {"ok": True}


@control_plane.post("/v1/proxy/register", response_model=ProxyRegisterResponse)
async def proxy_register(req: ProxyRegisterRequest):
    """
    Register/update a proxy idempotently.
    - If proxy_id is omitted, generate a new one
    - If proxy_id already exists, update host/port/endpoints and refresh last_seen
    """
    proxy_id = req.proxy_id or f"pxy_{uuid.uuid4().hex[:12]}"
    inst_cnt = int(req.instance_count or 0)
    kv_gb = float(req.kv_mem_per_instance_gb or 0.0)

    info = ProxyInfo(
        proxy_id=proxy_id,
        host=req.host,
        port=req.port,
        endpoints=list(req.endpoints or []),
        tags=list(req.tags or []),
        weight=float(req.weight),
        meta=dict(req.meta or {}),
        kv_cache_update_policy=str(req.kv_cache_update_policy or "lru"),
        load=ProxyLoad(max_capacity=int(req.max_capacity),
                       instance_count=int(req.instance_count or 0),
                       kv_mem_per_instance_gb=float(req.kv_mem_per_instance_gb or 0.0),
                       kv_cache_pool_gb=float(inst_cnt) * float(kv_gb),
                       ),  # Registration starts with an empty load; heartbeats update it later
        pool_resource=dict(req.pool_resource) if req.pool_resource is not None else None,
        pool_resource_reported_at=time.time() if req.pool_resource is not None else None,
    )
    # Registration is essentially an upsert
    pool = get_pool()
    await pool.upsert(info)

    logger.info(
        "proxy.register proxy_id=%s host=%s port=%s endpoints=%s",
        proxy_id, req.host, req.port, req.endpoints
    )

    return ProxyRegisterResponse(
        proxy_id=proxy_id,
        heartbeat_interval_s=HEARTBEAT_INTERVAL_S,
        ttl_s=CONTROL_PLANE_TTL_S,
    )


@control_plane.post("/v1/proxy/heartbeat")
async def proxy_heartbeat(req: ProxyHeartbeatRequest):
    """
    Heartbeat:
    - Refresh last_seen_at
    - If the request carries load fields, update load to keep the API extensible
    """
    load: Optional[ProxyLoad] = None
    # Update load if any field is provided; omitted fields are left unchanged, with default 0
    if req.inflight is not None or req.qps_1m is not None or req.gpu_util is not None:
        # Only update dynamic fields
        load = ProxyLoad(
            inflight=int(req.inflight) if req.inflight is not None else 0,
            qps_1m=float(req.qps_1m) if req.qps_1m is not None else 0.0,
            gpu_util=float(req.gpu_util) if req.gpu_util is not None else 0.0,
        )

    pool = get_pool()
    ok = await pool.heartbeat(req.proxy_id, load=load, meta_patch=req.meta_patch, pool_resource=req.pool_resource)
    if not ok:
        # Failure: output immediately and count it as an error
        await _hb_agg.record_proxy(req.proxy_id, ok=False)
        logger.warning("proxy.heartbeat rejected: proxy_id not registered, proxy_id=%s", req.proxy_id)
        raise HTTPException(status_code=404, detail="proxy_id not registered")

    # Success: aggregate only; do not spam logger.info per heartbeat
    await _hb_agg.record_proxy(
        req.proxy_id,
        ok=True,
        inflight=req.inflight,
        qps_1m=req.qps_1m,
        gpu_util=req.gpu_util,
    )
    return {"ok": True}


@control_plane.post("/v1/proxy/unregister")
async def proxy_unregister(req: ProxyUnregisterRequest):
    """Unregister: remove the proxy from the pool."""
    pool = get_pool()
    await pool.remove(req.proxy_id)
    logger.info("proxy.unregister proxy_id=%s", req.proxy_id)
    return {"ok": True}


@control_plane.get("/v1/proxy/list", response_model=List[ProxyInfoResponse])
async def proxy_list(include_dead: bool = False):
    """
    Query the proxy list:
    - include_dead=False：return only live proxies by default
    - include_dead=True：return all proxies, including inactive ones
    """
    pool = get_pool()
    infos = await pool.list(include_dead=include_dead)
    return [_to_response(x) for x in infos]


@control_plane.get("/debug/proxy_pool_resources")
async def debug_proxy_pool_resources(include_dead: bool = True):
    pool = get_pool()
    infos = await pool.list(include_dead=include_dead)
    return {
        "ok": True,
        "proxies": [
            {
                "proxy_id": p.proxy_id,
                "is_alive": p.is_alive(pool.ttl_s),
                "last_seen_at": p.last_seen_at,
                "pool_resource_reported_at": p.pool_resource_reported_at,
                "pool_resource": dict(p.pool_resource) if p.pool_resource is not None else None,
            }
            for p in infos
        ],
    }

# -------------------
# --- KDN-side methods ---
# -------------------
def _kdn_to_response(info: KDNInfo) -> KDNInfoResponse:
    pool = get_kdn_pool()
    return KDNInfoResponse(
        kdn_id=info.kdn_id,
        host=info.host,
        port=info.port,
        endpoints=list(info.endpoints),
        tags=list(info.tags),
        weight=float(info.weight),
        meta=dict(info.meta),
        items=int(info.load.items),
        qps_1m=float(info.load.qps_1m),
        pending_transfers=int(info.load.pending_transfers),
        active_transfers=int(info.load.active_transfers),
        network_queue_ms_ema=float(info.load.network_queue_ms_ema),
        registered_at=float(info.registered_at),
        last_seen_at=float(info.last_seen_at),
        is_alive=info.is_alive(pool.ttl_s),
    )

@control_plane.post("/v1/kdn/register", response_model=KDNRegisterResponse)
async def kdn_register(req: KDNRegisterRequest):
    pool = get_kdn_pool()
    info = KDNInfo(
        kdn_id=req.kdn_id,
        host=req.host,
        port=req.port,
        endpoints=list(req.endpoints or []),
        tags=list(req.tags or []),
        weight=float(req.weight),
        meta=dict(req.meta or {}),
        load=KDNLoad(),
    )
    await pool.upsert(info)
    logger.info("kdn.register kdn_id=%s host=%s port=%s endpoints=%s", req.kdn_id, req.host, req.port, req.endpoints)

    # fire-and-forget refresh trigger
    if _on_kdn_register is not None:
        try:
            asyncio.create_task(_on_kdn_register())
        except Exception:
            logger.exception("failed to trigger on_kdn_register callback")

    return KDNRegisterResponse(
        kdn_id=req.kdn_id,
        heartbeat_interval_s=HEARTBEAT_INTERVAL_S,
        ttl_s=CONTROL_PLANE_TTL_S,
    )

@control_plane.post("/v1/kdn/heartbeat")
async def kdn_heartbeat(req: KDNHeartbeatRequest):
    load = None
    if (
        req.items is not None
        or req.qps_1m is not None
        or req.pending_transfers is not None
        or req.active_transfers is not None
        or req.network_queue_ms_ema is not None
    ):
        load = KDNLoad(
            items=int(req.items or 0),
            qps_1m=float(req.qps_1m or 0.0),
            pending_transfers=int(req.pending_transfers or 0),
            active_transfers=int(req.active_transfers or 0),
            network_queue_ms_ema=float(req.network_queue_ms_ema or 0.0),
        )

    pool = get_kdn_pool()
    ok = await pool.heartbeat(req.kdn_id, load=load)
    if not ok:
        await _hb_agg.record_kdn(req.kdn_id, ok=False)
        logger.warning("kdn.heartbeat rejected: kdn_id not registered, kdn_id=%s", req.kdn_id)
        raise HTTPException(status_code=404, detail="kdn_id not registered")

    await _hb_agg.record_kdn(
        req.kdn_id,
        ok=True,
        items=req.items,
        qps_1m=req.qps_1m,
    )
    return {"ok": True}

@control_plane.post("/v1/kdn/unregister")
async def kdn_unregister(req: KDNUnregisterRequest):
    pool = get_kdn_pool()
    await pool.remove(req.kdn_id)
    logger.info("kdn.unregister kdn_id=%s", req.kdn_id)
    return {"ok": True}

@control_plane.get("/v1/kdn/list", response_model=List[KDNInfoResponse])
async def kdn_list(include_dead: bool = False):
    pool = get_kdn_pool()
    infos = await pool.list(include_dead=include_dead)
    return [_kdn_to_response(x) for x in infos]
