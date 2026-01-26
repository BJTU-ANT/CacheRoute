"""
Proxy_v1.py
---------
作为 Scheduler 的“下游代理”示例：

- 异步接收 Scheduler 转发的 Request payload（JSON）
- 简单解析其中的关键信息（Request_ID、Prompt、Service、Task 等）,还原为内部 Request 结构
- 基于 Request 中的信息，构造“OpenAI 风格”的 HTTP 请求体
- 调用下游 Instance
    * /v1/chat/completions  -> 流式 text/event-stream
    * /v1/completions       -> 非流式 JSON
- 将 Instance 的响应透传回 Scheduler（chat 为流式，completions 为一次性 JSON）

后续你可以在这里接入真正的 vLLM / OpenAI / 其它后端服务。
"""
from __future__ import annotations

import os
import json
import logging
from dataclasses import fields
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import JSONResponse, StreamingResponse

from core import Request as SchedulerRequest, Prompt, Service, Task
# from core.config import INSTANCE_BASE_URL
from core import forward_request


KDN_BASE_URL = os.environ.get("KDN_BASE_URL", "http://127.0.0.1:9101").rstrip("/")
INSTANCE_PORT = int(os.environ.get("INSTANCE_PORT", "9001"))

logger = logging.getLogger("proxy")
logging.basicConfig(level=logging.INFO)

proxy = FastAPI(title="CacheRoute Proxy v1")


# ======================= 公共内部处理函数 =======================
def _dataclass_from_dict(dc_cls, data: Dict[str, Any]):
    """
    安全地从 dict 构造 dataclass：
      - 只取 dataclass 中定义过的字段，避免因为多余字段报错
      - 必填字段如果缺失，会抛 TypeError，说明上游传的结构不对
    """
    if data is None:
        data = {}
    field_names = {f.name for f in fields(dc_cls)}
    filtered = {k: v for k, v in data.items() if k in field_names}
    return dc_cls(**filtered)


def recover_request_from_payload(payload: Dict[str, Any]) -> SchedulerRequest:
    """
        将 Scheduler 发送来的 JSON payload 恢复成 Request / Prompt / Service / Task 三个 dataclass。
    """
    req_id = payload.get("Request_ID", 0)
    req_type = payload.get("Request_type", "request")

    prompt_dict = payload.get("Prompt") or {}
    service_dict = payload.get("Service") or {}
    task_dict = payload.get("Task") or {}

    prompt_obj = _dataclass_from_dict(Prompt, prompt_dict)
    service_obj = _dataclass_from_dict(Service, service_dict)
    task_obj = _dataclass_from_dict(Task, task_dict)

    req_obj = SchedulerRequest(
        Request_ID=req_id,
        Request_type=req_type,
        Prompt=prompt_obj,
        Service=service_obj,
        Task=task_obj,
    )
    logger.info(
        "[Proxy] 恢复 Request 成功: Request_ID=%s, Endpoint_type=%s, model=%s",
        req_obj.Request_ID,
        getattr(req_obj.Service, "Endpoint_type", None),
        req_obj.Prompt.model,
    )
    return req_obj


def _format_retrieved_context(items: List[Dict[str, Any]]) -> str:
    """把 KDN 返回 items 格式化为可注入的检索上下文文本。"""
    lines = []
    for idx, it in enumerate(items, start=1):
        content = (it.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"({idx}) {content}")
    return "\n".join(lines).strip()


async def _fetch_knowledge_from_kdn(knowledge_ids: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    向 KDN 请求知识文本（非流式）。
    返回：(items, miss)
    """
    if not knowledge_ids:
        return [], []

    url = f"{KDN_BASE_URL}/knowledge/search/text"
    body = {
        "knowledge_ids": knowledge_ids,
        "need_fields": ["content", "length", "rel_path"],
    }

    content_bytes = b""
    async for chunk in forward_request(url, data=body, use_chunked=False):
        if chunk:
            content_bytes += chunk

    # forward_request 在 4xx/5xx 会抛 HTTPException，这里只处理 2xx 情况
    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = content_bytes.decode("utf-8", errors="ignore")

    try:
        resp = json.loads(text) if text else {}
    except json.JSONDecodeError:
        raise RuntimeError(f"KDN response is not valid JSON: {text[:200]}")

    items = resp.get("items") or []
    miss = resp.get("miss") or []
    if not isinstance(items, list):
        items = []
    if not isinstance(miss, list):
        miss = []

    return items, miss


def _inject_rag_into_instance_body(
    instance_body: Dict[str, Any],
    endpoint_type: str,
    retrieved_context: str,
) -> Dict[str, Any]:
    """
    将 retrieved_context 注入 instance_body（OpenAI 风格）。
    返回新的 body（不修改原 dict，避免副作用）。
    """
    if not retrieved_context:
        return instance_body

    new_body = dict(instance_body)

    if endpoint_type == "chat/completions":
        # messages 头插 system 消息
        msgs = list(new_body.get("messages") or [])
        system_prompt = (
            f"You are a helpful assistant.\n Use the following retrieved context to answer the user. If the context is not relevant, ignore it.\n ### Retrieved Context\n {retrieved_context}\n"
        )
        msgs.insert(0, {"role": "system", "content": system_prompt})
        new_body["messages"] = msgs
        return new_body

    # completions：前缀 prompt
    prompt = (new_body.get("prompt") or "")
    rag_prefix = (
        f"You are a helpful assistant.\n Use the following retrieved context to answer the user. If the context is not relevant, ignore it.\n ### Retrieved Context\n {retrieved_context}\n ### User Prompt\n"
    )
    new_body["prompt"] = rag_prefix + str(prompt)
    return new_body


def build_body_for_instance(req_obj: SchedulerRequest, mode: str) -> Dict[str, Any]:
    """
        根据 Request 构造发给 Instance 的 OpenAI 风格 body：
          - mode="chat"        -> /v1/chat/completions
          - mode="completions" -> /v1/completions
    """
    prompt = req_obj.Prompt
    model = prompt.model
    user_prompt = prompt.user_prompt
    max_tokens = getattr(prompt, "max_tokens", None)
    temperature = getattr(prompt, "temperature", None)
    top_p = getattr(prompt, "top_p", None)
    stream = getattr(prompt, "stream")
    # print(f"[Proxy]stream={stream}")

    if mode == "chat":
        # Instance 的 chat 接口按 OpenAI chat/completions 风格：
        # messages = [{role: "user", content: "..."}]
        body: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "user", "content": user_prompt},
            ],
            "stream": stream,
        }
    else:
        # completions：prompt + 非流式
        body = {
            "model": model,
            "prompt": user_prompt,
            "stream": False,
        }

        # 可选参数补上（有就带，没有就算了）
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p

    return body


# ======================= 本地代理方法路由 =======================
@proxy.post("/v1/chat/completions")
async def proxy_chat_completions(request: FastAPIRequest):
    """
    接收来自 Scheduler 的 /v1/chat/completions 请求（payload为 Request JSON）。
    转发为 OpenAI chat/completions body 到 Worker（流式）
    """
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception as e:
        logger.exception("[Proxy] chat/completions 解析 JSON 失败")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_json", "detail": str(e)},
        )

    # 恢复内部 Request
    try:
        req_obj = recover_request_from_payload(payload)
    except Exception as e:
        logger.exception("[Proxy] 恢复 Request 失败")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request_payload", "detail": str(e)},
        )

    # 构造 Instance 请求体
    instance_body = build_body_for_instance(req_obj, mode="chat")

    # 判断是否需要知识注入，若需要则先注入再转发
    enable_rag = bool(getattr(req_obj.Service, "Enable_know_injection", False))
    knowledge_ids = getattr(req_obj.Service, "Knowledge_List", []) or []
    if enable_rag and knowledge_ids:
        try:
            logger.info(f"[Proxy] 检测到启用知识注入，向{KDN_BASE_URL}获取知识")
            items, miss = await _fetch_knowledge_from_kdn([str(x) for x in knowledge_ids])
            retrieved_context = _format_retrieved_context(items)
            if miss:
                logger.info("[Proxy] KDN miss ids=%s", miss)

            # 注入到 instance_body
            instance_body = _inject_rag_into_instance_body(
                instance_body=instance_body,
                endpoint_type="chat/completions",
                retrieved_context=retrieved_context,
            )
            logger.info("[Proxy] RAG injected: ids=%s, ctx_len=%s", knowledge_ids, len(retrieved_context))
        except Exception as e:
            # 先跑通：KDN 出错时不阻断推理，退化为不注入直接转发
            logger.exception("[Proxy] KDN fetch failed, fallback to no-rag. err=%s", str(e))

    host = req_obj.Task.prefill_instance
    port = INSTANCE_PORT
    instance_url = f"http://{host}:{port}/v1/chat/completions"

    logger.info(
        "[Proxy] 转发到 Instance(chat): url=%s, model=%s",
        instance_url,
        json.dumps(instance_body,ensure_ascii=False)[:1200],
        # instance_body.get("model"),
    )

    # 下游流式：直接把 Instance 的 SSE 往上游透传
    try:
        stream_gen = forward_request(instance_url, data=instance_body, use_chunked=True)
        return StreamingResponse(stream_gen, media_type="text/event-stream")
    except Exception as e:
        logger.exception("[Proxy] 调用 Worker(chat) 失败")
        return JSONResponse(
            status_code=502,
            content={"error": "worker_chat_failed", "detail": str(e)},
        )



@proxy.post("/v1/completions")
async def proxy_completions(request: FastAPIRequest):
    """
    接收来自 Scheduler 的 /v1/completions 请求。
    Demo 里逻辑与 chat/completions 相同，只是留出扩展空间。
    """
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception as e:
        logger.exception("[Proxy] completions 解析 JSON 失败")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_json", "detail": str(e)},
        )

    # 恢复内部 Request
    try:
        req_obj = recover_request_from_payload(payload)
    except Exception as e:
        logger.exception("[Proxy] 恢复 Request 失败")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request_payload", "detail": str(e)},
        )

    # 构造 Instance 请求体
    instance_body = build_body_for_instance(req_obj, mode="completions")

    enable_rag = bool(getattr(req_obj.Service, "Enable_know_injection", False))
    knowledge_ids = getattr(req_obj.Service, "Knowledge_List", []) or []
    if enable_rag and knowledge_ids:
        try:
            logger.info(f"[Proxy] 检测到启用知识注入，向{KDN_BASE_URL}获取知识")
            items, miss = await _fetch_knowledge_from_kdn([str(x) for x in knowledge_ids])
            retrieved_context = _format_retrieved_context(items)
            instance_body = _inject_rag_into_instance_body(
                instance_body=instance_body,
                endpoint_type="completions",
                retrieved_context=retrieved_context,
            )
            logger.info("[Proxy] RAG injected: ids=%s, ctx_len=%s", knowledge_ids, len(retrieved_context))
        except Exception as e:
            logger.exception("[Proxy] KDN fetch failed, fallback to no-rag. err=%s", str(e))

    host = req_obj.Task.prefill_instance
    port = INSTANCE_PORT
    instance_url = f"http://{host}:{port}/v1/completions"

    logger.info(
        "[Proxy] 转发到 Instance(completions): url=%s, model=%s",
        instance_url,
        json.dumps(instance_body, ensure_ascii=False)[:1200],
        # instance_body.get("model"),
    )

    # 下游非流式：forward_request 仍然返回一个 async 生成器，但只会 yield 一次 bytes
    try:
        content_bytes = b""
        async for chunk in forward_request(instance_url, data=instance_body, use_chunked=False):
            if chunk:
                content_bytes += chunk
    except Exception as e:
        logger.exception("[Proxy] 调用 Worker(completions) 失败")
        return JSONResponse(
            status_code=502,
            content={"error": "worker_completions_failed", "detail": str(e)},
        )

    # 解析 Worker 返回内容
    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    try:
        resp_json = json.loads(text) if text else {}
        return JSONResponse(content=resp_json)
    except json.JSONDecodeError:
        # Worker 如果返回的不是 JSON，就直接把文本透传上去
        return JSONResponse(
            content={
                "raw_text": text,
                "parse_warning": "Worker 返回内容不是合法 JSON，已以文本形式透传。",
            }
        )
