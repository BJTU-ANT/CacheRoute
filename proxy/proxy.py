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
import asyncio
import logging
import uvicorn

from contextlib import asynccontextmanager
from dataclasses import fields
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import JSONResponse, StreamingResponse

from core import Request as SchedulerRequest, Prompt, Service, Task
# from core.config import INSTANCE_BASE_URL
from core import forward_request
from core import config

from proxy.sclient.scheduler_client import SchedulerControlClient
from proxy.resource.instance_pool import InstancePool
from proxy.resource import p_control_plane

SCHEDULER_CP_URL = os.environ.get("SCHEDULER_CP_URL", config.SCHEDULER_CP_URL).rstrip("/")
KDN_BASE_URL = os.environ.get("KDN_BASE_URL", config.KDN_BASE_URL).rstrip("/")

# PROXY_PORT = int(os.environ.get("PROXY_PORT", "8002"))
PROXY_ADVERTISE_HOST = os.environ.get("PROXY_ADVERTISE_HOST", config.PROXY_DP_HOST)
PROXY_ADVERTISE_PORT = int(os.environ.get("PROXY_ADVERTISE_PORT", str(config.PROXY_DP_PORT)))
PROXY_ID = os.environ.get("PROXY_ID", f"hp_{PROXY_ADVERTISE_HOST}:{PROXY_ADVERTISE_PORT}")
PROXY_HEARTBEAT_S = float(os.environ.get("PROXY_HEARTBEAT_S", config.HEARTBEAT_INTERVAL_S))

# NOTE:
# This is a TEMPORARY fallback for legacy request path.
# It MUST be removed once instance_pool-based routing is enabled.
INSTANCE_PORT = int(os.environ.get("INSTANCE_PORT", "9001"))

logger = logging.getLogger("proxy")
logging.basicConfig(level=logging.INFO)


# ======================= Proxy初始化 =======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Proxy 生命周期：
      - startup: 向 scheduler(control plane) 注册
      - running: 周期心跳，保证 proxy_pool 不过期
      - shutdown: 优雅注销（非强依赖，kill -9 情况靠 TTL 清理）
    """
    # --- 初始化实例池，并注入proxy控制平面 ---
    ttl_s = int(os.environ.get("PROXY_INSTANCE_TTL_S", config.INSTANCE_ALIVE_TTL_S))
    app.state.instance_pool = InstancePool(ttl_s=ttl_s)  # type: ignore
    p_control_plane.set_pool(app.state.instance_pool)  # type: ignore

    # ---尝试启动proxy控制平面，用于与Instance交互来动态刷新Instance池 ---
    cp_host = os.environ.get("PROXY_CP_HOST", config.PROXY_CP_HOST)
    cp_port = int(os.environ.get("PROXY_CP_PORT", config.PROXY_CP_PORT))

    cp_config = uvicorn.Config(
        p_control_plane.control_plane,
        host=cp_host,
        port=cp_port,
        log_level="info",
        # 重要：不要启用 reload / workers，embedded 场景保持单进程单实例
    )
    cp_server = uvicorn.Server(cp_config)
    app.state._cp_server = cp_server  # type: ignore

    async def _run_cp():
        await cp_server.serve()

    app.state._cp_task = asyncio.create_task(_run_cp())  # type: ignore
    logger.info("[Proxy] control plane started: http://%s:%s", cp_host, cp_port)

    # --- 启用scheduler客户端，尝试与scheduler交互并注册、与scheduler保活 ---
    client = SchedulerControlClient(SCHEDULER_CP_URL, timeout_s=5.0)
    app.state._sched_client = client  # type: ignore
    app.state._proxy_id = PROXY_ID    # type: ignore
    app.state._hb_stop = asyncio.Event()  # type: ignore

    # 1) register（失败不应阻塞业务启动：允许 proxy 单独跑）
    try:
        reg = await client.register(
            proxy_id=PROXY_ID,
            host=PROXY_ADVERTISE_HOST,
            port=PROXY_ADVERTISE_PORT,
            endpoints=["chat/completions", "completions"],
            meta={"version": "proxy_v1"},
        )
        # 用 scheduler 建议的心跳周期覆盖本地默认
        interval = float(reg.heartbeat_interval_s) if reg.heartbeat_interval_s else PROXY_HEARTBEAT_S
        app.state._hb_interval = interval  # type: ignore
        logger.info("[Proxy] registered to scheduler: cp=%s proxy_id=%s advertise=%s:%s hb=%ss",
                    SCHEDULER_CP_URL, reg.proxy_id, PROXY_ADVERTISE_HOST, PROXY_ADVERTISE_PORT, interval)
    except Exception as e:
        # 不阻塞业务面：注册失败时 proxy 仍可本地转发（只是 scheduler 看不到它）
        app.state._hb_interval = PROXY_HEARTBEAT_S  # type: ignore
        logger.warning("[Proxy] register failed (non-fatal): cp=%s err=%s", SCHEDULER_CP_URL, str(e))

    # 2) heartbeat loop
    async def _hb_loop():
        while not app.state._hb_stop.is_set():  # type: ignore
            try:
                # 先不做 load 上报，后续你扩展 inflight/gpu_util 时再接入
                await client.heartbeat(proxy_id=PROXY_ID)
            except Exception as e:
                logger.warning("[Proxy] heartbeat failed: err=%s", str(e))
            await asyncio.sleep(float(getattr(app.state, "_hb_interval", PROXY_HEARTBEAT_S)))  # type: ignore

    app.state._hb_task = asyncio.create_task(_hb_loop())  # type: ignore

    try:
        yield
    finally:
        # 关闭控制平面
        try:
            srv = getattr(app.state, "_cp_server", None)  # type: ignore
            t = getattr(app.state, "_cp_task", None)  # type: ignore
            if srv is not None:
                srv.should_exit = True
                srv.force_exit = True
            if t is not None:
                # 不要长时间 await；给它一个很短的机会退出即可
                try:
                    await asyncio.wait_for(t, timeout=2.0)
                except Exception:
                    t.cancel()
        except Exception:
            pass

        # 向scheduler汇报
        try:
            app.state._hb_stop.set()  # type: ignore
            task = getattr(app.state, "_hb_task", None)  # type: ignore
            if task:
                task.cancel()
        except Exception:
            pass

        try:
            await client.unregister(proxy_id=PROXY_ID)
            logger.info("[Proxy] unregistered from scheduler: proxy_id=%s", PROXY_ID)
        except Exception as e:
            logger.warning("[Proxy] unregister failed (ignore): err=%s", str(e))

        try:
            await client.close()
        except Exception:
            pass

proxy = FastAPI(title="CacheRoute Proxy v1", lifespan=lifespan)


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
