# instance/kv_service.py
"""
给 Instance 控制平面复用，负责调用 KDN 的运行时注入接口。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx


class KDNRuntimeClient:
    def __init__(self, timeout_s: float = 30.0):
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def close(self) -> None:
        await self._client.aclose()

    async def inject_ready_kv(
        self,
        kdn_addr: str,
        request_id: int,
        model: str,
        knowledge_ids: List[str],
        redis_host: str,
        redis_port: int,
        redis_db: int,
        redis_password: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        调 KDN 运行时接口，只对已 kv_ready 的 kid 做 Redis 注入。
        """
        if not kdn_addr:
            raise ValueError("kdn_addr is empty")

        kdn_addr = kdn_addr.strip()
        if kdn_addr.startswith("http://") or kdn_addr.startswith("https://"):
            base_url = kdn_addr.rstrip("/")
        else:
            # 默认补 http://，端口由上游决定是否已携带
            base_url = f"http://{kdn_addr}".rstrip("/")

        payload = {
            "request_id": int(request_id),
            "model": model,
            "knowledge_ids": [str(x) for x in (knowledge_ids or [])],
            "redis_host": redis_host,
            "redis_port": int(redis_port),
            "redis_db": int(redis_db),
            "redis_password": redis_password,
        }

        r = await self._client.post(f"{base_url}/knowledge/inject_ready_kv", json=payload)
        r.raise_for_status()
        return r.json()