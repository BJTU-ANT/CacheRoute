"""Lightweight browser UI server for CacheRoute Proxy observability."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PROXY_CP_URL = os.environ.get("PROXY_UI_PROXY_CP_URL", "http://127.0.0.1:8002").rstrip("/")
DEFAULT_SCHEDULER_CP_URL = os.environ.get("PROXY_UI_SCHEDULER_CP_URL", "http://127.0.0.1:7002").rstrip("/")
DEFAULT_PROXY_ID = os.environ.get("PROXY_UI_PROXY_ID", os.environ.get("PROXY_ID", "")).strip()
DEFAULT_SCHEDULER_PROXY_LIST_PATH = os.environ.get("PROXY_UI_SCHEDULER_PROXY_LIST_PATH", "/v1/proxy/list")
REQUEST_TIMEOUT_S = float(os.environ.get("PROXY_UI_REQUEST_TIMEOUT_S", "3.0"))

app = FastAPI(title="CacheRoute Proxy UI", version="v1")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def _get_json(base_url: str, path: str, params: Dict[str, Any] | None = None) -> Any:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail={"url": url, "error": str(exc)}) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"url": url, "error": str(exc)}) from exc


@app.get("/api/config")
async def config() -> Dict[str, Any]:
    return {
        "proxy_cp_url": DEFAULT_PROXY_CP_URL,
        "scheduler_cp_url": DEFAULT_SCHEDULER_CP_URL,
        "proxy_id": DEFAULT_PROXY_ID,
        "scheduler_proxy_list_path": DEFAULT_SCHEDULER_PROXY_LIST_PATH,
        "poll_interval_ms": int(os.environ.get("PROXY_UI_POLL_INTERVAL_MS", "3000")),
    }


@app.get("/api/proxy/healthz")
async def proxy_healthz() -> Any:
    return await _get_json(DEFAULT_PROXY_CP_URL, "/healthz")


@app.get("/api/proxy/status")
async def proxy_status() -> Any:
    return await _get_json(DEFAULT_PROXY_CP_URL, "/debug/status")


@app.get("/api/proxy/instances")
async def proxy_instances(include_dead: bool = Query(True)) -> Any:
    return await _get_json(DEFAULT_PROXY_CP_URL, "/v1/instance/list", {"include_dead": str(include_dead).lower()})


@app.get("/api/proxy/resources")
async def proxy_resources(include_dead: bool = Query(True)) -> Any:
    return await _get_json(DEFAULT_PROXY_CP_URL, "/debug/instance_resources", {"include_dead": str(include_dead).lower()})


@app.get("/api/proxy/topology")
async def proxy_topology() -> Any:
    return await _get_json(DEFAULT_PROXY_CP_URL, "/v1/topology/kdn_links")


@app.get("/api/proxy/loads")
async def proxy_loads() -> Any:
    return await _get_json(DEFAULT_PROXY_CP_URL, "/debug/instance_loads", {"include_dead": "true"})


@app.get("/api/scheduler/proxy")
async def scheduler_proxy() -> Dict[str, Any]:
    if not DEFAULT_PROXY_ID:
        return {"ok": False, "error": "proxy_id_not_configured"}
    items = await _get_json(DEFAULT_SCHEDULER_CP_URL, DEFAULT_SCHEDULER_PROXY_LIST_PATH, {"include_dead": "true"})
    if not isinstance(items, list):
        return {"ok": False, "error": "unexpected_scheduler_payload", "payload": items}
    for item in items:
        if str(item.get("proxy_id", "")) == DEFAULT_PROXY_ID:
            return {"ok": True, "proxy": item}
    return {"ok": False, "error": "proxy_not_found", "proxy_id": DEFAULT_PROXY_ID}
