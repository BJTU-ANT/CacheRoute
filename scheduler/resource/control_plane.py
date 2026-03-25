# scheduler/resource/control_plane.py
# -*- coding: utf-8 -*-
"""
Scheduler 控制平面（Control Plane）：

职责：
- 对外提供 proxy 注册 / 心跳 / 注销 / 查询 API
- 将状态写入 ProxyPool（资源池）
- 不包含调度策略、不与 7001 数据平面耦合

运行方式：
- 你当前的架构：7001 scheduler 启动时，在 lifespan 里额外起一个 uvicorn Server 监听 7002
- control_plane 这个 FastAPI app 会在同进程运行，因此天然共享内存（同一个 ProxyPool 实例）

注意：
- 如果使用多 worker，会出现“每个进程一份 ProxyPool”，且 7002 会端口冲突。
  因此该设计默认 scheduler 单进程单 worker。
"""
from __future__ import annotations

import os
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
# Pydantic 请求/响应模型
# ----------------------------

class ProxyRegisterRequest(BaseModel):
    """
    Proxy 注册请求。

    说明：
    - proxy_id 可选：不传则由控制平面生成
    - host/port：Scheduler 能访问到 proxy 的地址
    - endpoints：proxy 支持的转发端点（OpenAI 风格 path 片段）
    - tags/weight/meta：先存起来，后续调度策略可用
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

class ProxyRegisterResponse(BaseModel):
    """
    注册响应：
    - proxy_id：控制平面确认的唯一 ID
    - heartbeat_interval_s：建议心跳周期
    - ttl_s：失活阈值
    """
    proxy_id: str
    heartbeat_interval_s: int
    ttl_s: int


class ProxyHeartbeatRequest(BaseModel):
    """
    心跳请求：目前只需要 proxy_id
    未来扩展：可以在这里新增 load 字段，上报 inflight/gpu_util 等。
    """
    proxy_id: str

    # 未来可扩展负载上报（你准备好时打开）
    inflight: Optional[int] = None
    qps_1m: Optional[float] = None
    gpu_util: Optional[float] = None


class ProxyUnregisterRequest(BaseModel):
    proxy_id: str


class ProxyInfoResponse(BaseModel):
    """
    对外查询返回的 proxy 信息。
    用 Pydantic 模型是为了接口稳定，且避免直接暴露内部 dataclass 对象。
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
# 对外调用池方法
# ----------------------------
def set_pool(pool: ProxyPool) -> None:
    """由 scheduler(7001) 在 startup 时注入共享的 ProxyPool 实例。"""
    global _pool
    _pool = pool

def get_pool() -> ProxyPool:
    """控制平面与数据平面都通过它拿到同一份池。"""
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
    """触发KDNrefresh更新用"""
    global _on_kdn_register
    _on_kdn_register = cb

# ----------------------------
# FastAPI app + 资源池实例
# ----------------------------


control_plane = FastAPI(title="CacheRoute Scheduler Control Plane v1")

_HB_REPORT_INTERVAL_S = int(os.environ.get("SCHEDULER_HB_REPORT_INTERVAL_S", "30"))
_hb_agg = HeartbeatLogAggregator()

@control_plane.on_event("startup")
async def _hb_report_startup():
    async def _get_proxies() -> List[ProxyInfo]:
        # 取“池内当前状态”，包含 load/last_seen
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
# 单进程共享资源池：控制平面 API 写入；未来调度策略从同一个 pool 读取
# _pool = ProxyPool(ttl_s=CONTROL_PLANE_TTL_S)

# -------------------
# --- Proxy 侧方法 ---
# -------------------
def _to_response(info: ProxyInfo) -> ProxyInfoResponse:
    """内部 dataclass -> 对外响应模型的转换。"""
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
    )


@control_plane.get("/healthz")
async def healthz():
    """
    健康检查：用于验证 7002 是否成功启动、路由可达。
    """
    return {"ok": True}


@control_plane.post("/v1/proxy/register", response_model=ProxyRegisterResponse)
async def proxy_register(req: ProxyRegisterRequest):
    """
    注册/更新 proxy（幂等）。
    - 如果 proxy_id 不传：生成一个新的
    - 如果 proxy_id 已存在：更新 host/port/endpoints 等，并刷新 last_seen
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
                       ),  # 注册阶段先给空负载，后续由心跳更新
    )
    # 注册本质是 upsert
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
    心跳：
    - 刷新 last_seen_at
    - 如果请求里携带负载字段，则更新 load（保持可扩展）
    """
    load: Optional[ProxyLoad] = None
    # 只要有任一字段给了，就更新 load（没给的不改，默认 0）
    if req.inflight is not None or req.qps_1m is not None or req.gpu_util is not None:
        # 仅更新动态字段
        load = ProxyLoad(
            inflight=int(req.inflight) if req.inflight is not None else 0,
            qps_1m=float(req.qps_1m) if req.qps_1m is not None else 0.0,
            gpu_util=float(req.gpu_util) if req.gpu_util is not None else 0.0,
        )

    pool = get_pool()
    ok = await pool.heartbeat(req.proxy_id, load=load)
    if not ok:
        # 失败：立刻输出（并计入 err）
        await _hb_agg.record_proxy(req.proxy_id, ok=False)
        logger.warning("proxy.heartbeat rejected: proxy_id not registered, proxy_id=%s", req.proxy_id)
        raise HTTPException(status_code=404, detail="proxy_id not registered")

    # 成功：只聚合，不逐条 logger.info 刷屏
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
    """注销：从池中移除 proxy。"""
    pool = get_pool()
    await pool.remove(req.proxy_id)
    logger.info("proxy.unregister proxy_id=%s", req.proxy_id)
    return {"ok": True}


@control_plane.get("/v1/proxy/list", response_model=List[ProxyInfoResponse])
async def proxy_list(include_dead: bool = False):
    """
    查询 proxy 列表：
    - include_dead=False：只返回存活 proxy（默认）
    - include_dead=True：返回全部（含失活）
    """
    pool = get_pool()
    infos = await pool.list(include_dead=include_dead)
    return [_to_response(x) for x in infos]

# -------------------
# --- KDN 侧方法 ---
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
