# scheduler/control_plane.py
"""
调度器的控制平面，用于资源信息收集
注册、心跳、注销、列表、健康检查。后续调度策略用它来拿可用 proxy 集合。
它不会单独启动，而是伴随scheduler lifespan启动接受处理。
"""
from __future__ import annotations

import os
import time
import uuid
import asyncio

import logging
logger = logging.getLogger("scheduler.control_plane")

from typing import Any, Dict, List, Optional, cast

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from core import config


CONTROL_PLANE_TTL_S = int(os.environ.get("SCHEDULER_PROXY_TTL_S", config.CONTROL_PLANE_TTL_S))
HEARTBEAT_INTERVAL_S = int(os.environ.get("SCHEDULER_PROXY_HEARTBEAT_S", config.HEARTBEAT_INTERVAL_S))


class ProxyRegisterRequest(BaseModel):
    proxy_id: Optional[str] = Field(default=None)
    host: str
    port: int = Field(..., ge=1, le=65535)
    endpoints: List[str] = Field(default_factory=lambda: ["chat/completions", "completions"])
    tags: List[str] = Field(default_factory=list)
    weight: float = Field(default=1.0, ge=0.0)
    meta: Dict[str, Any] = Field(default_factory=dict)


class ProxyRegisterResponse(BaseModel):
    proxy_id: str
    heartbeat_interval_s: int
    ttl_s: int


class ProxyHeartbeatRequest(BaseModel):
    proxy_id: str


class ProxyUnregisterRequest(BaseModel):
    proxy_id: str


class ProxyInfo(BaseModel):
    proxy_id: str
    host: str
    port: int
    endpoints: List[str]
    tags: List[str]
    weight: float
    meta: Dict[str, Any]
    registered_at: float
    last_seen_at: float
    is_alive: bool


class ProxyRegistry:
    def __init__(self, ttl_s: int):
        self.ttl_s = ttl_s
        self._lock = asyncio.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}

    def _now(self) -> float:
        return time.time()

    def _is_alive(self, rec: Dict[str, Any], now: Optional[float] = None) -> bool:
        now = now or self._now()
        return (now - float(rec["last_seen_at"])) <= self.ttl_s

    async def register(self, req: ProxyRegisterRequest) -> str:
        async with self._lock:
            now = self._now()
            proxy_id = req.proxy_id or f"pxy_{uuid.uuid4().hex[:12]}"

            rec = self._data.get(proxy_id)
            if rec is None:
                rec = {"proxy_id": proxy_id, "registered_at": now}

            rec = cast(Dict[str, Any], rec)

            rec.update(
                host=req.host,
                port=req.port,
                endpoints=list(req.endpoints or []),
                tags=list(req.tags or []),
                weight=float(req.weight),
                meta=dict(req.meta or {}),
                last_seen_at=now,
            )
            self._data[proxy_id] = rec
            return proxy_id

    async def heartbeat(self, proxy_id: str) -> None:
        async with self._lock:
            rec = self._data.get(proxy_id)
            if not rec:
                raise KeyError(proxy_id)
            rec["last_seen_at"] = self._now()

    async def unregister(self, proxy_id: str) -> None:
        async with self._lock:
            self._data.pop(proxy_id, None)

    async def list(self, include_dead: bool = False) -> List[ProxyInfo]:
        async with self._lock:
            now = self._now()
            out: List[ProxyInfo] = []
            for rec in self._data.values():
                alive = self._is_alive(rec, now=now)
                if (not include_dead) and (not alive):
                    continue
                out.append(
                    ProxyInfo(
                        proxy_id=rec["proxy_id"],
                        host=rec["host"],
                        port=rec["port"],
                        endpoints=list(rec.get("endpoints") or []),
                        tags=list(rec.get("tags") or []),
                        weight=float(rec.get("weight", 1.0)),
                        meta=dict(rec.get("meta") or {}),
                        registered_at=float(rec.get("registered_at", now)),
                        last_seen_at=float(rec.get("last_seen_at", 0.0)),
                        is_alive=alive,
                    )
                )
            out.sort(key=lambda x: x.last_seen_at, reverse=True)
            return out


control_plane = FastAPI(title="CacheRoute Scheduler Control Plane v1")
_registry = ProxyRegistry(ttl_s=CONTROL_PLANE_TTL_S)


@control_plane.get("/healthz")
async def healthz():
    return {"ok": True}


@control_plane.post("/v1/proxy/register", response_model=ProxyRegisterResponse)
async def proxy_register(req: ProxyRegisterRequest):
    proxy_id = await _registry.register(req)
    logger.info(
        "[Scheduler] proxy.register proxy_id=%s host=%s port=%s endpoints=%s tags=%s weight=%s",
        proxy_id, req.host, req.port, req.endpoints, req.tags, req.weight
    )
    return ProxyRegisterResponse(
        proxy_id=proxy_id,
        heartbeat_interval_s=HEARTBEAT_INTERVAL_S,
        ttl_s=CONTROL_PLANE_TTL_S,
    )


@control_plane.post("/v1/proxy/heartbeat")
async def proxy_heartbeat(req: ProxyHeartbeatRequest):
    try:
        await _registry.heartbeat(req.proxy_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="proxy_id not registered")
    logger.info("[Scheduler] proxy.heartbeat proxy_id=%s", req.proxy_id)
    return {"ok": True}


@control_plane.post("/v1/proxy/unregister")
async def proxy_unregister(req: ProxyUnregisterRequest):
    await _registry.unregister(req.proxy_id)
    logger.info("[Scheduler] proxy.unregister proxy_id=%s", req.proxy_id)
    return {"ok": True}


@control_plane.get("/v1/proxy/list", response_model=List[ProxyInfo])
async def proxy_list(include_dead: bool = False):
    return await _registry.list(include_dead=include_dead)
