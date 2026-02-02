# kdn_server/sclient/scheduler_client.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
import httpx


class SchedulerClient:
    def __init__(self, scheduler_cp_url: str, timeout_s: float = 5.0) -> None:
        self.base = scheduler_cp_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self._cli = httpx.AsyncClient(timeout=self.timeout_s)

    async def close(self) -> None:
        await self._cli.aclose()

    async def register(
        self,
        kdn_id: str,
        host: str,
        port: int,
        endpoints: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        weight: float = 1.0,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base}/v1/kdn/register"
        payload = {
            "kdn_id": kdn_id,
            "host": host,
            "port": int(port),
            "endpoints": endpoints or ["knowledge/snapshot", "knowledge/search/text"],
            "tags": tags or [],
            "weight": float(weight),
            "meta": meta or {},
        }
        r = await self._cli.post(url, json=payload)
        r.raise_for_status()
        return r.json()

    async def heartbeat(self, kdn_id: str) -> Dict[str, Any]:
        url = f"{self.base}/v1/kdn/heartbeat"
        r = await self._cli.post(url, json={"kdn_id": kdn_id})
        r.raise_for_status()
        return r.json()

    async def unregister(self, kdn_id: str) -> Dict[str, Any]:
        url = f"{self.base}/v1/kdn/unregister"
        r = await self._cli.post(url, json={"kdn_id": kdn_id})
        r.raise_for_status()
        return r.json()
