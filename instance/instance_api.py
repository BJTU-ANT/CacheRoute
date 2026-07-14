"""
instance.py

Instance adaptation layer between the vLLM instance and Proxy:
  - Receives OpenAI-style HTTP requests from Proxy and always exposes stable /v1/chat/completions, /v1/completions, and /control/*** endpoints for Proxy/Scheduler compatibility;
  - Forwards user requests to real vLLM or another backend engine and passes vLLM responses upstream as-is whenever possible;
  - /v1/completions: returns a non-streaming JSON response with prompt-like completion output;
  - /v1/chat/completions: returns a preset streaming response (text/event-stream).

!!! Note:
  The current version only simulates inference. Later, connect the vLLM generator here and replace the mock logic below.
"""

from __future__ import annotations

import uvicorn,logging
import os
import asyncio
import json
import subprocess
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import JSONResponse, StreamingResponse

from core import forward_request, config
from .mock_resp import (mock_text_completion,
                        mock_chat_completion,
                        mock_chat_stream
                        )
from util import parse_stream_flag
from instance import control_plane
from instance.pclient.proxy_client import ProxyControlClient


PROXY_CP_URL = os.environ.get("PROXY_CP_URL", config.PROXY_CP_URL).rstrip("/")
INSTANCE_ADVERTISE_HOST = os.environ.get("INSTANCE_ADVERTISE_HOST", config.INSTANCE_HOST)
INSTANCE_ADVERTISE_PORT = int(os.environ.get("INSTANCE_ADVERTISE_PORT", os.environ.get("INSTANCE_PORT", config.INSTANCE_PORT)))
INSTANCE_ID = os.environ.get("INSTANCE_ID", f"hp_{INSTANCE_ADVERTISE_HOST}:{INSTANCE_ADVERTISE_PORT}")

vllm_base_url = config.VLLM_BASE_URL.rstrip("/")
use_mock = True if config.USE_MOCK else False


def _norm_http_base(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = f"http://{s}"
    p = urlparse(s)
    if not p.hostname or not p.port:
        return ""
    return f"{p.scheme or 'http'}://{p.hostname}:{p.port}"


def _discover_iface_for_host(host: str) -> Optional[str]:
    try:
        out = subprocess.check_output(["ip", "route", "get", host], text=True, stderr=subprocess.STDOUT)
        toks = out.strip().split()
        if "dev" in toks:
            idx = toks.index("dev")
            if idx + 1 < len(toks):
                return toks[idx + 1]
    except Exception:
        return None
    return None


def _read_iface_speed_mbps(iface: str) -> Optional[float]:
    if not iface:
        return None
    speed_path = f"/sys/class/net/{iface}/speed"
    try:
        with open(speed_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        speed = float(raw)
        return speed if speed > 0 else None
    except Exception:
        return None


async def _probe_kdn_link(client: ProxyControlClient, instance_id: str, target: str, logger: logging.Logger) -> Optional[Tuple[str, Dict[str, Any]]]:
    base = _norm_http_base(target)
    if not base:
        return None
    parsed = urlparse(base)
    host = str(parsed.hostname or "")
    port = int(parsed.port or 0)
    if not host or port <= 0:
        return None

    import httpx
    timeout = httpx.Timeout(2.0, connect=2.0)
    async with httpx.AsyncClient(timeout=timeout) as hc:
        try:
            hello = await hc.post(
                f"{base}/v1/topology/hello",
                json={
                    "instance_id": instance_id,
                    "instance_host": INSTANCE_ADVERTISE_HOST,
                    "instance_port": INSTANCE_ADVERTISE_PORT,
                },
            )
            hello.raise_for_status()
            hj = hello.json()
        except Exception as e:
            logger.warning("[Instance] topology hello failed target=%s err=%s", base, e)
            return None

        rtts: List[float] = []
        for _ in range(3):
            t0 = asyncio.get_running_loop().time()
            ping = await hc.get(f"{base}/v1/topology/ping")
            ping.raise_for_status()
            rtts.append((asyncio.get_running_loop().time() - t0) * 1000.0)
        latency_ms = sum(rtts) / len(rtts) if rtts else 0.0

    iface = _discover_iface_for_host(host) or ""
    bw = _read_iface_speed_mbps(iface)
    if bw is None:
        bw = float(os.environ.get("INSTANCE_DEFAULT_LINK_BW_MBPS", str(getattr(config, "INSTANCE_DEFAULT_LINK_BW_MBPS", 1000.0))) or 1000.0)
    kdn_id = str(hj.get("kdn_id") or f"kdn://{host}:{port}")
    metrics = {
        "bandwidth_mbps": round(float(bw), 3),
        "latency_ms": round(float(latency_ms), 3),
        "iface": iface or None,
        "measured_by": instance_id,
    }
    return kdn_id, metrics


async def _run_topology_discovery(client: ProxyControlClient, instance_id: str, logger: logging.Logger) -> None:
    raw_targets = os.environ.get("INSTANCE_TOPOLOGY_KDN_TARGETS", getattr(config, "INSTANCE_TOPOLOGY_KDN_TARGETS", "")).strip()
    if not raw_targets:
        return
    targets = [x.strip() for x in raw_targets.split(",") if x.strip()]
    if not targets:
        return

    links: Dict[str, Dict[str, Any]] = {}
    for t in targets:
        result = await _probe_kdn_link(client, instance_id=instance_id, target=t, logger=logger)
        if result is None:
            continue
        kdn_id, metrics = result
        links[kdn_id] = metrics
        parsed = urlparse(_norm_http_base(t))
        links[f"kdn://{parsed.hostname}:{parsed.port}"] = dict(metrics)

    if not links:
        return
    try:
        resp = await client.report_kdn_topology(instance_id=instance_id, links=links)
        logger.info("[Instance] topology report ok links=%s resp=%s", len(links), resp)
    except Exception as e:
        logger.warning("[Instance] topology report failed links=%s err=%s", len(links), e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = ProxyControlClient(PROXY_CP_URL, timeout_s=5.0)
    stop = asyncio.Event()
    app.state._pc = client # type: ignore
    app.state._stop = stop # type: ignore
    cp_host = os.environ.get("INSTANCE_CP_HOST", config.INSTANCE_CP_HOST)
    cp_port = int(os.environ.get("INSTANCE_CP_PORT", config.INSTANCE_CP_PORT))
    logger = logging.getLogger("instance")

    cp_config = uvicorn.Config(
        control_plane.control_plane,
        host=cp_host,
        port=cp_port,
        log_level="info",
    )
    cp_server = uvicorn.Server(cp_config)
    app.state._cp_server = cp_server  # type: ignore

    async def _run_cp():
        await cp_server.serve()

    app.state._cp_task = asyncio.create_task(_run_cp())  # type: ignore
    logger.info("[Instance] control plane started: http://%s:%s", cp_host, cp_port)

    try:
        reg = await client.register(
            instance_id=INSTANCE_ID,
            host=INSTANCE_ADVERTISE_HOST,
            port=INSTANCE_ADVERTISE_PORT,
            endpoints=["chat/completions", "completions"],
            meta={"version": "instance_v1"},
        )
        runtime_instance_id = reg.instance_id
        interval = float(reg.heartbeat_interval_s) if reg.heartbeat_interval_s else 10.0
        print(
            f"[Instance] registered to proxy_cp={PROXY_CP_URL} "
            f"id={reg.instance_id} advertise={INSTANCE_ADVERTISE_HOST}:{INSTANCE_ADVERTISE_PORT} "
            f"hb={reg.heartbeat_interval_s}s ttl={reg.ttl_s}s"
        )
    except Exception as e:
        # Do not block Instance startup: Proxy cannot see it if registration fails, but Instance can still run.
        interval = 10.0
        runtime_instance_id = INSTANCE_ID
        print(f"[Instance][WARN] register failed: proxy_cp={PROXY_CP_URL} err={e}")

    async def _hb():
        fail = 0
        while not stop.is_set():
            try:
                await client.heartbeat(runtime_instance_id)
                fail = 0
            except Exception as e:
                fail += 1
                # Log once every 6 failures to avoid noisy output.
                if fail % 6 == 0:
                    print(f"[Instance][WARN] heartbeat failed x{fail}: proxy_cp={PROXY_CP_URL} err={e}")
            await asyncio.sleep(interval)

    task = asyncio.create_task(_hb())
    app.state._hb_task = task # type: ignore

    resource_monitor = getattr(app.state, "_demo_resource_monitor", None)  # type: ignore
    registration_ok = runtime_instance_id == getattr(reg, "instance_id", None) if "reg" in locals() else False
    if resource_monitor is not None:
        if registration_ok:
            await resource_monitor.start_after_registration(runtime_instance_id=runtime_instance_id, stop_event=stop, logger=logger)
        else:
            resource_monitor.skip_after_registration_failure(logger=logger)

    app.state._topology_task = asyncio.create_task(  # type: ignore
        _run_topology_discovery(client=client, instance_id=runtime_instance_id, logger=logger)
    )

    try:
        yield
    finally:
        stop.set()
        try:
            task.cancel()
        except Exception:
            pass
        try:
            topo_task = getattr(app.state, "_topology_task", None)  # type: ignore
            if topo_task is not None:
                topo_task.cancel()
        except Exception:
            pass
        try:
            resource_monitor = getattr(app.state, "_demo_resource_monitor", None)  # type: ignore
            if resource_monitor is not None:
                await resource_monitor.stop(logger=logger)
        except Exception:
            pass
        try:
            await client.unregister(runtime_instance_id)
        except Exception:
            pass
        try:
            await client.close()
        except Exception:
            pass
        try:
            srv = getattr(app.state, "_cp_server", None)  # type: ignore
            t = getattr(app.state, "_cp_task", None)  # type: ignore
            if srv is not None:
                srv.should_exit = True
                srv.force_exit = True
            if t is not None:
                try:
                    await asyncio.wait_for(t, timeout=2.0)
                except Exception:
                    t.cancel()
        except Exception:
            pass

instance = FastAPI(title="Instance v1", lifespan=lifespan)





# ============== Basic configuration and mode switches ==============

def _use_real_vllm() -> bool:
    """Return whether real vLLM mode is enabled."""
    return (not use_mock) and bool(vllm_base_url)

# ========================= Handle vLLM responses with unknown formats =========================
def _safe_json_from_bytes(content_bytes: bytes) -> Dict[str, Any]:
    """Try to parse bytes as JSON; wrap with raw_text if parsing fails."""
    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = content_bytes.decode("utf-8", errors="ignore")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_text": text}


# ================ vLLM interface ===============
async def _vllm_stream_chat(payload: Dict[str, Any]) -> AsyncGenerator[bytes, None]:
    """
    Call real vLLM /v1/chat/completions in stream mode and return the SSE byte stream as-is.
    """
    if not _use_real_vllm():
        # Fall back to mock.
        print("Simulated streaming chat response")
        async for chunk in mock_chat_stream(payload):
            yield chunk
        return

    print(f"[Instance] Sending to vLLM instance: {vllm_base_url} and waiting for response")
    assert forward_request is not None
    url = f"{vllm_base_url}/v1/chat/completions"
    # Forward the OpenAI-style body from Proxy directly.
    upstream_stream = forward_request(url, data=payload, use_chunked=True)  # type: ignore

    async for chunk in upstream_stream:
        # Do not parse or rewrite; pass through directly.
        if chunk:
            yield chunk


async def _vllm_chat_completion(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Call real vLLM /v1/chat/completions in non-streaming mode and return JSON.
    """
    if not _use_real_vllm():
        print("Simulated non-streaming chat response")
        return await mock_chat_completion(payload)

    print(f"[Instance] Sending to vLLM instance: {vllm_base_url} and waiting for response")
    assert forward_request is not None
    url = f"{vllm_base_url}/v1/chat/completions"

    content_bytes = b""
    async for chunk in forward_request(url, data=payload, use_chunked=False):  # type: ignore
        if chunk:
            content_bytes += chunk

    return _safe_json_from_bytes(content_bytes)


async def _vllm_text_completion(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Call real vLLM /v1/completions in non-streaming mode and return JSON.
    """
    if not _use_real_vllm():
        print("Simulated completion response")
        return await mock_text_completion(payload)

    print(f"[Instance] Sending to vLLM instance: {vllm_base_url} and waiting for response")
    assert forward_request is not None
    url = f"{vllm_base_url}/v1/completions"

    content_bytes = b""
    async for chunk in forward_request(url, data=payload, use_chunked=False):  # type: ignore
        if chunk:
            content_bytes += chunk

    return _safe_json_from_bytes(content_bytes)


# ======================= Scheduler method routes =======================
@instance.post("/v1/chat/completions")
async def instance_chat_completions(request: FastAPIRequest):
    """
    chat/completions：
      - Request body format: {"model": "...", "messages": [...], "stream": bool, ...}
      - stream=True  : return an SSE stream
      - stream=False : return one JSON response
    """
    try:
        print(f"USE_MOCK={use_mock},VLLM={vllm_base_url},_use_real_vllm={_use_real_vllm()}")
        payload: Dict[str, Any] = await request.json()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_json", "detail": str(e)},
        )

    stream = parse_stream_flag(payload.get("stream"))
    print(f"[Instance] stream={stream}")

    # Streaming: always use SSE byte streams.
    if stream:
        async def event_stream():
            async for chunk in _vllm_stream_chat(payload):
                yield chunk

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    resp_json = await _vllm_chat_completion(payload)
    return JSONResponse(content=resp_json)


@instance.post("/v1/completions")
async def instance_completions(request: FastAPIRequest):
    """
    completions：
      - Request body format: {"model": "...", "prompt": "...", ...}
      - Current version is non-streaming only; implement streaming here following chat/completions if needed.
    """
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_json", "detail": str(e)},
        )

    resp_json = await _vllm_text_completion(payload)
    return JSONResponse(content=resp_json)

