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
# import time

from typing import Any, AsyncGenerator, Dict
from contextlib import asynccontextmanager
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

    try:
        yield
    finally:
        stop.set()
        try:
            task.cancel()
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

