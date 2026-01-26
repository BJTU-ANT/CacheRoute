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

# import os
# import asyncio
import json
# import time

from typing import Any, AsyncGenerator, Dict
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import JSONResponse, StreamingResponse

from core import forward_request
from .mock_resp import (mock_text_completion,
                        mock_chat_completion,
                        mock_chat_stream
                        )
from core.config import USE_MOCK,VLLM_BASE_URL
from util import parse_stream_flag

instance = FastAPI(title="Instance v1")

vllm_base_url = VLLM_BASE_URL.rstrip("/")
use_mock = True if USE_MOCK else False



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

