# proxy/resource/p_control_plane.py

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from .instance_pool import InstancePool

import logging
logger = logging.getLogger("proxy.p_control_plane")


_control_plane = FastAPI(title="CacheRoute Proxy Control Plane", version="v1")
_pool: Optional[InstancePool] = None


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


@_control_plane.get("/healthz")
async def healthz() -> Dict[str, Any]:
    pool = get_pool()
    return {"ok": True, "ttl_s": pool.ttl_s}


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


@_control_plane.post("/v1/instance/unregister")
async def unregister(req: InstanceUnregisterReq) -> Dict[str, Any]:
    pool = get_pool()
    ok = pool.remove(req.instance_id)
    logger.info("[ProxyCP] instance unregister: id=%s ok=%s", req.instance_id, ok)
    return {"ok": ok}


@_control_plane.get("/v1/instance/list")
async def list_instances(include_dead: bool = False) -> List[Dict[str, Any]]:
    pool = get_pool()
    items = pool.list(include_dead=include_dead)
    out: List[Dict[str, Any]] = []
    for it in items:
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
            # 由 include_dead 决定返回集合，alive 在这里标记方便调试
            "is_alive": True if not include_dead else None,
        })
    return out


# 对外导出 app
control_plane = _control_plane
