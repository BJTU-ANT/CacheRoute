# proxy/sclient/scheduler_client.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx


@dataclass
class RegisterResult:
    proxy_id: str
    heartbeat_interval_s: int
    ttl_s: int


class SchedulerControlClient:
    """
    Proxy -> Scheduler(Control Plane) 的客户端封装：
      - register / heartbeat / unregister
    只负责协议交互，不关心 Proxy 的业务转发。
    """

    def __init__(self, base_url: str, timeout_s: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def close(self) -> None:
        await self._client.aclose()

    async def register(
        self,
        proxy_id: str,
        host: str,
        port: int,
        endpoints: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        weight: float = 1.0,
        meta: Optional[Dict[str, Any]] = None,
        max_capacity: int = 0,
        instance_count: int = 0,
        kv_mem_per_instance_gb: float = 0.0,
        kv_cache_update_policy: str = "lru",
    ) -> RegisterResult:
        payload = {
            "proxy_id": proxy_id,
            "host": host,
            "port": port,
            "endpoints": endpoints or ["chat/completions", "completions"],
            "tags": tags or [],
            "weight": weight,
            "meta": meta or {},
            "max_capacity": max_capacity,
            "instance_count": instance_count,
            "kv_mem_per_instance_gb": kv_mem_per_instance_gb,
            "kv_cache_update_policy": kv_cache_update_policy,
        }
        r = await self._client.post(f"{self.base_url}/v1/proxy/register", json=payload)
        r.raise_for_status()
        j = r.json()
        return RegisterResult(
            proxy_id=j["proxy_id"],
            heartbeat_interval_s=int(j.get("heartbeat_interval_s", 10)),
            ttl_s=int(j.get("ttl_s", 30)),
        )

    async def heartbeat(
        self,
        proxy_id: str,
        inflight: Optional[int] = None,
        qps_1m: Optional[float] = None,
        gpu_util: Optional[float] = None,
        meta_patch: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {"proxy_id": proxy_id}
        # 只在有值时携带，避免触发服务端“全量覆盖为 0”
        if inflight is not None:
            payload["inflight"] = inflight
        if qps_1m is not None:
            payload["qps_1m"] = qps_1m
        if gpu_util is not None:
            payload["gpu_util"] = gpu_util
        if meta_patch:
            payload["meta_patch"] = meta_patch

        r = await self._client.post(f"{self.base_url}/v1/proxy/heartbeat", json=payload)
        r.raise_for_status()

    async def unregister(self, proxy_id: str) -> None:
        r = await self._client.post(f"{self.base_url}/v1/proxy/unregister", json={"proxy_id": proxy_id})
        r.raise_for_status()
