"""
instance.py

vllm实例与proxy之间的适配层instance：
  - 接收来自 Proxy 的 OpenAI 风格 HTTP 请求，
    始终暴露稳定的 /v1/chat/completions、/v1/completions 和 /control/***，兼容 Proxy/Scheduler；
  - 将用户请求转发到真实 vLLM（或后端引擎），无论 vLLM 返回什么格式，都尽量“按原样”往上透传
  - /v1/completions：返回一个非流式 JSON，字段为 prompt（模拟 completion 输出）
  - /v1/chat/completions：返回预设的流式响应（text/event-stream）

!!! 注意：
  当前版本只是“假装推理”，后续你只需要在这里对接 vLLM 的 generator，
  把下面的 mock 逻辑替换掉即可。
"""

from __future__ import annotations

import uvicorn,logging
import os
import asyncio
import json
import subprocess
import signal
import time
import shutil
import urllib.request
from collections import deque
# import time

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
from instance.resource_agent.proxy_reporter import report_once


PROXY_CP_URL = os.environ.get("PROXY_CP_URL", config.PROXY_CP_URL).rstrip("/")
INSTANCE_ADVERTISE_HOST = os.environ.get("INSTANCE_ADVERTISE_HOST", config.INSTANCE_HOST)
INSTANCE_ADVERTISE_PORT = int(os.environ.get("INSTANCE_ADVERTISE_PORT", os.environ.get("INSTANCE_PORT", config.INSTANCE_PORT)))
INSTANCE_ID = os.environ.get("INSTANCE_ID", f"hp_{INSTANCE_ADVERTISE_HOST}:{INSTANCE_ADVERTISE_PORT}")

vllm_base_url = config.VLLM_BASE_URL.rstrip("/")
use_mock = True if config.USE_MOCK else False


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _resource_report_interval_ms() -> int:
    raw_interval = os.environ.get("INSTANCE_RESOURCE_REPORT_INTERVAL_MS")
    if raw_interval:
        try:
            return max(1, int(raw_interval))
        except ValueError:
            pass
    hz = float(os.environ.get("INSTANCE_RESOURCE_REPORT_HZ", config.INSTANCE_RESOURCE_REPORT_HZ) or 30.0)
    return max(1, int(round(1000.0 / max(hz, 0.001))))


def _agent_listen() -> str:
    return os.environ.get("INSTANCE_RESOURCE_AGENT_LISTEN", config.INSTANCE_RESOURCE_AGENT_LISTEN).strip()


def _agent_url() -> str:
    return os.environ.get("INSTANCE_RESOURCE_AGENT_URL", config.INSTANCE_RESOURCE_AGENT_URL).rstrip("/")


def _read_tail(lines: deque[str]) -> str:
    return "".join(lines).strip()


def _spawn_log_reader(stream, tail: deque[str]) -> None:
    if stream is None:
        return
    try:
        for raw in iter(stream.readline, b""):
            if not raw:
                break
            tail.append(raw.decode("utf-8", errors="replace"))
    except Exception:
        return


def _agent_health_ok(agent_url: str, timeout_s: float = 1.0) -> bool:
    try:
        req = urllib.request.Request(f"{agent_url.rstrip('/')}/healthz", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return 200 <= int(resp.status) < 300
    except Exception:
        return False


async def _wait_agent_ready(agent_url: str, timeout_s: float, logger: logging.Logger) -> bool:
    deadline = time.monotonic() + max(0.1, timeout_s)
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_agent_health_ok, agent_url, 1.0):
            logger.info("[InstanceResource] agent ready: %s", agent_url)
            return True
        await asyncio.sleep(0.2)
    return False


async def _start_resource_agent(runtime_instance_id: str, logger: logging.Logger):
    agent_url = _agent_url()
    if await asyncio.to_thread(_agent_health_ok, agent_url, 1.0):
        logger.info("[InstanceResource] reuse reachable resource agent: %s", agent_url)
        return None, True

    if not _env_bool("INSTANCE_RESOURCE_AUTO_START_AGENT", config.INSTANCE_RESOURCE_AUTO_START_AGENT):
        logger.warning("[InstanceResource] agent not reachable and auto-start disabled: %s", agent_url)
        return None, False

    cargo = shutil.which("cargo")
    if cargo is None:
        logger.warning("[InstanceResource] cargo not found; resource agent cannot auto-start")
        return None, False

    listen = _agent_listen()
    sample_ms = int(os.environ.get("INSTANCE_RESOURCE_AGENT_SAMPLE_INTERVAL_MS", config.INSTANCE_RESOURCE_AGENT_SAMPLE_INTERVAL_MS))
    cmd = [
        cargo,
        "run",
        "--quiet",
        "--manifest-path",
        str(ROOT_DIR / "instance" / "resource_agent" / "Cargo.toml"),
        "--",
        "--listen",
        listen,
        "--sample-interval-ms",
        str(sample_ms),
        "--instance-id",
        runtime_instance_id,
    ]
    stdout_tail: deque[str] = deque(maxlen=20)
    stderr_tail: deque[str] = deque(maxlen=20)
    logger.info("[InstanceResource] starting resource agent: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except Exception as exc:
        logger.warning("[InstanceResource] failed to start resource agent cmd=%s err=%s", " ".join(cmd), exc)
        return None, False

    asyncio.create_task(asyncio.to_thread(_spawn_log_reader, proc.stdout, stdout_tail))
    asyncio.create_task(asyncio.to_thread(_spawn_log_reader, proc.stderr, stderr_tail))

    timeout_s = float(os.environ.get("INSTANCE_RESOURCE_AGENT_START_TIMEOUT_S", config.INSTANCE_RESOURCE_AGENT_START_TIMEOUT_S))
    ready = await _wait_agent_ready(agent_url, timeout_s=timeout_s, logger=logger)
    if ready:
        return proc, True

    rc = proc.poll()
    logger.warning(
        "[InstanceResource] resource agent startup failed or timed out: cmd=%s returncode=%s stdout_tail=%r stderr_tail=%r",
        " ".join(cmd),
        rc,
        _read_tail(stdout_tail),
        _read_tail(stderr_tail),
    )
    _stop_resource_agent(proc, logger)
    return None, False


def _stop_resource_agent(proc, logger: logging.Logger) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    except Exception as exc:
        logger.debug("[InstanceResource] resource agent cleanup ignored: %s", exc)


async def _resource_report_loop(stop: asyncio.Event, runtime_instance_id: str, logger: logging.Logger) -> None:
    agent_url = _agent_url()
    interval_ms = _resource_report_interval_ms()
    timeout_s = float(os.environ.get("INSTANCE_RESOURCE_REPORT_TIMEOUT_S", config.INSTANCE_RESOURCE_REPORT_TIMEOUT_S))
    interval_s = max(0.001, interval_ms / 1000.0)
    logger.info(
        "[InstanceResource] reporting enabled: agent_url=%s proxy_cp=%s interval_ms=%s timeout_s=%s",
        agent_url,
        PROXY_CP_URL,
        interval_ms,
        timeout_s,
    )
    failures = 0
    first_ok = False
    while not stop.is_set():
        try:
            ok = await asyncio.to_thread(
                report_once,
                agent_url,
                PROXY_CP_URL,
                runtime_instance_id,
                timeout_s,
                False,
                False,
            )
            if ok:
                failures = 0
                if not first_ok:
                    first_ok = True
                    logger.info("[InstanceResource] first resource report ok: instance_id=%s", runtime_instance_id)
            else:
                failures += 1
                if failures == 1 or failures % 30 == 0:
                    logger.warning("[InstanceResource] resource report rejected x%s instance_id=%s", failures, runtime_instance_id)
        except Exception as exc:
            failures += 1
            if failures == 1 or failures % 30 == 0:
                logger.warning("[InstanceResource] resource report failed x%s err=%s", failures, exc)
        await asyncio.sleep(interval_s)


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
        # 不阻塞 instance 业务启动：注册失败时 proxy 看不到，但 instance 自己仍可跑
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
                # 每 6 次失败打一次，避免刷屏
                if fail % 6 == 0:
                    print(f"[Instance][WARN] heartbeat failed x{fail}: proxy_cp={PROXY_CP_URL} err={e}")
            await asyncio.sleep(interval)

    task = asyncio.create_task(_hb())
    app.state._hb_task = task # type: ignore

    app.state._resource_agent_proc = None  # type: ignore
    app.state._resource_report_task = None  # type: ignore
    monitor_enabled = _env_bool("INSTANCE_RESOURCE_MONITOR_ENABLE", config.INSTANCE_RESOURCE_MONITOR_ENABLE)
    report_enabled = _env_bool("INSTANCE_RESOURCE_REPORT_ENABLE", config.INSTANCE_RESOURCE_REPORT_ENABLE)
    if monitor_enabled:
        logger.info("[InstanceResource] monitor enabled: agent_url=%s", _agent_url())
        agent_proc, agent_ready = await _start_resource_agent(runtime_instance_id, logger)
        app.state._resource_agent_proc = agent_proc  # type: ignore
        if agent_ready and report_enabled:
            app.state._resource_report_task = asyncio.create_task(  # type: ignore
                _resource_report_loop(stop=stop, runtime_instance_id=runtime_instance_id, logger=logger)
            )
        elif not agent_ready:
            logger.warning("[InstanceResource] reporting disabled because resource agent is not ready")
        else:
            logger.info("[InstanceResource] resource reporting disabled")
    else:
        logger.info("[InstanceResource] monitor disabled")

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
            rpt_task = getattr(app.state, "_resource_report_task", None)  # type: ignore
            if rpt_task is not None:
                rpt_task.cancel()
        except Exception:
            pass
        try:
            _stop_resource_agent(getattr(app.state, "_resource_agent_proc", None), logger)  # type: ignore
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





# ============== 基本配置 & 模式开关 ==============

def _use_real_vllm() -> bool:
    """当前是否启用真实 vLLM 模式"""
    return (not use_mock) and bool(vllm_base_url)

# ========================= 处理“未知格式”的 vLLM 返回 =========================
def _safe_json_from_bytes(content_bytes: bytes) -> Dict[str, Any]:
    """尝试把 bytes 解析成 JSON；失败则用 raw_text 包装一层"""
    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = content_bytes.decode("utf-8", errors="ignore")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_text": text}


# ================ vLLM 接口 ===============
async def _vllm_stream_chat(payload: Dict[str, Any]) -> AsyncGenerator[bytes, None]:
    """
    调用真实 vLLM 的 /v1/chat/completions（stream 模式），
    并把 SSE 字节流原样返回。
    """
    if not _use_real_vllm():
        # 回退到 mock
        print("模拟流式chat回复")
        async for chunk in mock_chat_stream(payload):
            yield chunk
        return

    print(f"[Instance] 发送到vllm实例：{vllm_base_url}，等待响应")
    assert forward_request is not None
    url = f"{vllm_base_url}/v1/chat/completions"
    # 直接把 Proxy 给过来的 OpenAI 风格 body 转发下去
    upstream_stream = forward_request(url, data=payload, use_chunked=True)  # type: ignore

    async for chunk in upstream_stream:
        # 不解析、不改写，直接透传
        if chunk:
            yield chunk


async def _vllm_chat_completion(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    调用真实 vLLM 的 /v1/chat/completions（非流式），返回 JSON。
    """
    if not _use_real_vllm():
        print("模拟非流式chat回复")
        return await mock_chat_completion(payload)

    print(f"[Instance] 发送到vllm实例：{vllm_base_url}，等待响应")
    assert forward_request is not None
    url = f"{vllm_base_url}/v1/chat/completions"

    content_bytes = b""
    async for chunk in forward_request(url, data=payload, use_chunked=False):  # type: ignore
        if chunk:
            content_bytes += chunk

    return _safe_json_from_bytes(content_bytes)


async def _vllm_text_completion(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    调用真实 vLLM 的 /v1/completions（非流式），返回 JSON。
    """
    if not _use_real_vllm():
        print("模拟completion回复")
        return await mock_text_completion(payload)

    print(f"[Instance] 发送到vllm实例：{vllm_base_url}，等待响应")
    assert forward_request is not None
    url = f"{vllm_base_url}/v1/completions"

    content_bytes = b""
    async for chunk in forward_request(url, data=payload, use_chunked=False):  # type: ignore
        if chunk:
            content_bytes += chunk

    return _safe_json_from_bytes(content_bytes)


# ======================= 调度器方法路由 =======================
@instance.post("/v1/chat/completions")
async def instance_chat_completions(request: FastAPIRequest):
    """
    chat/completions：
      - 请求体格式：{"model": "...", "messages": [...], "stream": bool, ...}
      - stream=True  : 返回 SSE 流
      - stream=False : 返回一次性 JSON
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

    # 流式：统一走 SSE 字节流
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
      - 请求体格式：{"model": "...", "prompt": "...", ...}
      - 当前版本仅非流式，如需流式可在此仿照 chat/completions 实现
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

