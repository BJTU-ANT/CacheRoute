# instance/control_plane.py
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from core import config
from instance.kv_service import KDNRuntimeClient

logger = logging.getLogger("instance.control_plane")

_control_plane = FastAPI(title="CacheRoute Instance Control Plane", version="v1")


class KVInjectReadyReq(BaseModel):
    request_id: int
    kdn_addr: str
    model: str
    knowledge_ids: List[str] = []


@_control_plane.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {"ok": True}


@_control_plane.post("/v1/kv/inject_ready")
async def inject_ready(req: KVInjectReadyReq) -> Dict[str, Any]:
    """
    仅负责把 Proxy 的控制请求转给 KDN。
    Instance 自己提供目标 Redis 参数。
    """
    redis_host = os.environ.get("INSTANCE_REDIS_HOST", getattr(config, "INSTANCE_REDIS_HOST", "127.0.0.1"))
    redis_port = int(os.environ.get("INSTANCE_REDIS_PORT", str(getattr(config, "INSTANCE_REDIS_PORT", 6379))))
    redis_db = int(os.environ.get("INSTANCE_REDIS_DB", str(getattr(config, "INSTANCE_REDIS_DB", 0))))
    redis_password = os.environ.get("INSTANCE_REDIS_PASSWORD", getattr(config, "INSTANCE_REDIS_PASSWORD", None))

    cli = KDNRuntimeClient(timeout_s=60.0)
    try:
        result = await cli.inject_ready_kv(
            kdn_addr=req.kdn_addr,
            request_id=req.request_id,
            model=req.model,
            knowledge_ids=req.knowledge_ids,
            redis_host=redis_host,
            redis_port=redis_port,
            redis_db=redis_db,
            redis_password=redis_password,
        )

        logger.info(
            "[InstanceCP] rid=%s inject_ready ok=%s injected=%s text_only=%s miss=%s keys=%s",
            req.request_id,
            result.get("ok"),
            result.get("injected_kids", []),
            result.get("text_only_kids", []),
            result.get("miss_kids", []),
            result.get("keys_injected", 0),
        )
        return result

    finally:
        await cli.close()


control_plane = _control_plane