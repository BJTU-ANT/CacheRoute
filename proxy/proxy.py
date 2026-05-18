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
from typing import Any, Dict, List, Tuple, AsyncGenerator

from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import JSONResponse, StreamingResponse

from core import Request as SchedulerRequest, Prompt, Service, Task
# from core.config import INSTANCE_BASE_URL
from core import forward_request
from core import config

from proxy.sclient.scheduler_client import SchedulerControlClient
from proxy.resource.instance_pool import InstancePool
from proxy.resource import p_control_plane
from proxy.resource.hb_log import HeartbeatReporter, hb_report_loop
from proxy.strategy.factory import build_instance_strategy
from proxy.queue import QueueManager, ProxyTask

SCHEDULER_CP_URL = os.environ.get("SCHEDULER_CP_URL", config.SCHEDULER_CP_URL).rstrip("/")
# KDN_BASE_URL = os.environ.get("KDN_BASE_URL", config.KDN_BASE_URL).rstrip("/")

# PROXY_PORT = int(os.environ.get("PROXY_PORT", "8002"))
PROXY_ADVERTISE_HOST = os.environ.get("PROXY_ADVERTISE_HOST", config.PROXY_DP_HOST)
PROXY_ADVERTISE_PORT = int(os.environ.get("PROXY_ADVERTISE_PORT", str(config.PROXY_DP_PORT)))
PROXY_ID = os.environ.get("PROXY_ID", f"hp_{PROXY_ADVERTISE_HOST}:{PROXY_ADVERTISE_PORT}")
PROXY_HEARTBEAT_S = float(os.environ.get("PROXY_HEARTBEAT_S", config.HEARTBEAT_INTERVAL_S))
PROXY_MAX_CAPACITY = int(os.environ.get("PROXY_MAX_CAPACITY", config.PROXY_MAX_CAPACITY))
PROXY_INSTANCE_COUNT = int(os.environ.get("PROXY_INSTANCE_COUNT", config.PROXY_INSTANCE_COUNT))
PROXY_KV_MEM_PER_INSTANCE_GB = float(os.environ.get("PROXY_KV_MEM_PER_INSTANCE_GB", config.PROXY_KV_MEM_PER_INSTANCE_GB))
PROXY_KV_CACHE_UPDATE_POLICY = os.environ.get("PROXY_KV_CACHE_UPDATE_POLICY", config.PROXY_KV_CACHE_UPDATE_POLICY)
PROXY_INJECTION_STRATEGY = os.environ.get("PROXY_INJECTION_STRATEGY", "default").strip().lower()
if PROXY_INJECTION_STRATEGY not in {"default", "iws"}:
    logger.warning(
        "[Proxy] invalid PROXY_INJECTION_STRATEGY=%s, fallback to default",
        PROXY_INJECTION_STRATEGY,
    )
    PROXY_INJECTION_STRATEGY = "default"

# NOTE:
# This is a TEMPORARY fallback for legacy request path.
# It MUST be removed once instance_pool-based routing is enabled.
INSTANCE_PORT = int(os.environ.get("INSTANCE_PORT", "9001"))

logger = logging.getLogger("proxy")
logging.basicConfig(level=logging.INFO)


def _load_static_kdn_links() -> Dict[str, Any]:
    raw_links = os.environ.get("PROXY_KDN_LINKS_JSON", "").strip()
    if not raw_links:
        return {}
    try:
        parsed = json.loads(raw_links)
        if isinstance(parsed, dict):
            return parsed
        logger.warning("[Proxy] PROXY_KDN_LINKS_JSON is not dict, ignored")
    except Exception as e:
        logger.warning("[Proxy] parse PROXY_KDN_LINKS_JSON failed: %s", e)
    return {}


async def _build_proxy_topology_meta() -> Dict[str, Any]:
    static_links = _load_static_kdn_links()
    dynamic_links = await p_control_plane.get_kdn_links_snapshot()
    merged_links = dict(static_links)
    merged_links.update(dynamic_links)
    return {"kdn_links": merged_links} if merged_links else {}


def _squelch_noisy_loggers():
    # http client
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # uvicorn access log（可选，避免每次请求一行）
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # 如果你用了 asyncio/anyio 也很吵，再加：
    # logging.getLogger("asyncio").setLevel(logging.WARNING)
    # logging.getLogger("anyio").setLevel(logging.WARNING)

# ======================= Proxy初始化 =======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Proxy 生命周期：
      - startup: 向 scheduler(control plane) 注册
      - running: 周期心跳，保证 proxy_pool 不过期
      - shutdown: 优雅注销（非强依赖，kill -9 情况靠 TTL 清理）
    """
    _squelch_noisy_loggers()
    app.state.injection_strategy_name = PROXY_INJECTION_STRATEGY  # type: ignore
    logger.info("[Proxy] injection strategy=%s", app.state.injection_strategy_name)
    # --- 初始化实例池，并注入proxy控制平面 ---
    ttl_s = int(os.environ.get("PROXY_INSTANCE_TTL_S", config.INSTANCE_ALIVE_TTL_S))
    app.state.instance_pool = InstancePool(ttl_s=ttl_s)  # type: ignore
    p_control_plane.set_pool(app.state.instance_pool)  # type: ignore

    # --- 加载proxy调度策略（业务面使用） ---
    strategy_name = os.environ.get("PROXY_INSTANCE_STRATEGY", "round_robin")
    try:
        app.state.instance_strategy = build_instance_strategy(strategy_name)  # type: ignore
        logger.info("[Proxy] instance strategy=%s", strategy_name)
    except Exception as e:
        # 策略初始化失败是致命的（否则业务面无法选择 instance）
        logger.error("[Proxy] invalid instance strategy=%s err=%s", strategy_name, str(e))
        raise

    # ---尝试启动proxy控制平面，用于与Instance交互来动态刷新Instance池 ---
    cp_host = os.environ.get("PROXY_CP_HOST", config.PROXY_CP_HOST)
    cp_port = int(os.environ.get("PROXY_CP_PORT", config.PROXY_CP_PORT))

    cp_config = uvicorn.Config(
        p_control_plane.control_plane,
        host=cp_host,
        port=cp_port,
        log_level="info",
        access_log=False,
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

    # --- 心跳日志聚合（输出层）---
    app.state._hb_reporter = HeartbeatReporter(interval_s=30.0)  # type: ignore
    app.state._hb_report_task = asyncio.create_task(  # type: ignore
        hb_report_loop(
            reporter=app.state._hb_reporter,  # type: ignore
            logger=logger,
            proxy_id=PROXY_ID,
            stop_event=app.state._hb_stop,  # type: ignore
        )
    )

    # 1) register（失败不应阻塞业务启动：允许 proxy 单独跑）
    try:
        # CacheRoute 第二阶段：
        # 可选注入 KDN->Proxy 静态拓扑信息，供 Scheduler 词典序策略使用。
        # 环境变量示例：
        # PROXY_KDN_LINKS_JSON='{"kdn_a":{"bandwidth_tier":3,"latency_tier":1}}'
        proxy_meta: Dict[str, Any] = {"version": "proxy_v1"}
        proxy_meta.update(await _build_proxy_topology_meta())

        reg = await client.register(
            proxy_id=PROXY_ID,
            host=PROXY_ADVERTISE_HOST,
            port=PROXY_ADVERTISE_PORT,
            endpoints=["chat/completions", "completions"],
            meta=proxy_meta,
            max_capacity=PROXY_MAX_CAPACITY,
            instance_count=PROXY_INSTANCE_COUNT,
            kv_mem_per_instance_gb=PROXY_KV_MEM_PER_INSTANCE_GB,
            kv_cache_update_policy=PROXY_KV_CACHE_UPDATE_POLICY,
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
        reporter: HeartbeatReporter = app.state._hb_reporter  # type: ignore
        while not app.state._hb_stop.is_set():  # type: ignore
            try:
                await client.heartbeat(
                    proxy_id=PROXY_ID,
                    meta_patch=await _build_proxy_topology_meta(),
                )
                await reporter.record(ok=True)
            except Exception as e:
                # 不逐条 warning，避免刷屏；只记录窗口统计
                await reporter.record(ok=False, err=str(e))
                # 真要立即看到异常堆栈：你可以改成 logger.debug(..., exc_info=True)
                logger.debug("[Proxy] heartbeat failed", exc_info=True)

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
            rpt = getattr(app.state, "_hb_report_task", None)  # type: ignore
            if rpt:
                rpt.cancel()
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
queue_mgr = QueueManager()              #创建任务队列管理器


#--------------------------------------------------------------
# ======================= 公共内部处理函数 =======================
#--------------------------------------------------------------

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
    stream = getattr(prompt, "stream", False)
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


def _sse_meta_event(task: ProxyTask) -> bytes:
    payload = {
        "trace": task.trace,
        "kv_ack": task.kv_ack,
        "kv_ready_kids": task.kv_ready_kids,
        "text_only_kids": task.text_only_kids,
        "miss_kids": task.miss_kids,
        "error": task.error,
    }
    return (
        "event: cacheroute_meta\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


async def _wrap_chat_stream_with_meta(task: ProxyTask, queue_mgr: QueueManager) -> AsyncGenerator[bytes, None]:
    """
    转发下游 chat SSE，但把 [DONE] 延后，先插入一条 cacheroute_meta 事件。
    """
    pending = b""
    done_seen = False

    async for chunk in queue_mgr.iter_response(task):
        if not chunk:
            continue

        pending += chunk

        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            full_line = line + b"\n"

            if line.startswith(b"data:"):
                data = line[len(b"data:"):].strip()
                if data == b"[DONE]":
                    done_seen = True
                    continue

            yield full_line

        # 保守处理：如果 chunk 里没有换行，继续积累

    if pending:
        # 还有残留未结束行，原样转发
        yield pending
        pending = b""

    # 在 [DONE] 之前插入 meta
    yield _sse_meta_event(task)

    # 再发真正的 done
    if done_seen:
        yield b"data: [DONE]\n\n"
    else:
        # 即便下游没发，也补一个，避免 client 一直等
        yield b"data: [DONE]\n\n"


def select_instance(app: FastAPI, req_obj: SchedulerRequest):
    """
    业务面选择一个 instance。
    - 输入：当前存活实例列表（由 InstancePool 提供）
    - 输出：一个 InstanceInfo（至少有 host/port/instance_id）
    """
    pool = app.state.instance_pool  # type: ignore
    strategy = app.state.instance_strategy  # type: ignore

    instances = pool.list(include_dead=False)
    if not instances:
        return None

    try:
        chosen = strategy.select(instances, hint=req_obj)
        return chosen
    except Exception as e:
        logger.warning("[Proxy] instance select failed: err=%s", str(e))
        return None


#--------------------------------------------------------------
# ======================= 本地代理方法路由 =======================
#--------------------------------------------------------------

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

    chosen = select_instance(proxy, req_obj)
    if not chosen:
        return JSONResponse(
            status_code=503,
            content={"error": "no_instance", "detail": "proxy has no alive instance"},
        )

    host = chosen.host
    port = int(chosen.port)
    url_path = "/v1/chat/completions"
    logger.info("[Proxy] instance chosen(chat): id=%s addr=%s:%s", getattr(chosen, "instance_id", "?"), host, port)
    strategy_name = getattr(proxy.state, "injection_strategy_name", "default")
    if strategy_name == "iws":
        original_mode = getattr(req_obj.Service, "Injection_type", "text")
        try:
            costs = await queue_mgr.estimate_iws_costs(
                req_obj=req_obj,
                instance_id=chosen.instance_id,
                kdn_addr=getattr(req_obj.Task, "KDN_server_addr", None),
            )
            if costs.get("kv_hidden_by_ready_wait"):
                iws_suggest = "kvcache"
            elif (costs.get("kvcache_total_ms") or 0) < (costs.get("text_total_ms") or 0):
                iws_suggest = "kvcache"
            else:
                iws_suggest = "text"
            logger.info(
                "[Proxy][IWS][DryRun] rid=%s original=%s iws_suggest=%s applied=%s ready_wait=%s "
                "kv_prepare=%s kv_hidden=%s text_total=%s kvcache_total=%s "
                "text_service=%s kvcache_service=%s kv_transfer=%s kv_queue_wait=%s "
                "redis_load=%s residual_prefill=%s effective_len=%s residual_tokens=%s "
                "bw=%s bw_src=%s",
                req_obj.Request_ID,
                original_mode,
                iws_suggest,
                original_mode,
                costs.get("ready_wait_ms"),
                costs.get("kvcache_prepare_ms"),
                costs.get("kv_hidden_by_ready_wait"),
                costs.get("text_total_ms"),
                costs.get("kvcache_total_ms"),
                costs.get("text_service_ms"),
                costs.get("kvcache_service_ms"),
                costs.get("kv_transfer_ms"),
                costs.get("kv_queue_wait_ms"),
                costs.get("redis_load_ms"),
                costs.get("residual_prefill_ms"),
                costs.get("effective_knowledge_len"),
                costs.get("residual_tokens"),
                costs.get("bandwidth_mbps"),
                costs.get("bandwidth_source"),
            )
        except Exception as e:
            logger.warning(
                "[Proxy][IWS][DryRun] estimate_iws_costs failed rid=%s err=%s keep=%s",
                req_obj.Request_ID,
                str(e),
                original_mode,
            )

    # ====================================
    # 送入队列enqueue -> manager -> forward
    # ====================================
    try:
        # 1) 封装任务（注：chosen 来自 RR，具备 instance_id/host/port 字段 :contentReference[oaicite:5]{index=5}）
        task = ProxyTask(
            request_id=getattr(req_obj, "Request_ID", None),
            req_obj=req_obj,
            instance_body=instance_body,
            instance_id=chosen.instance_id,
            instance_host=chosen.host,
            instance_port=int(chosen.port),
            kdn_addr=getattr(req_obj.Task, "KDN_server_addr", None),
            url_path=url_path,
        )

        await queue_mgr.enqueue_prepare(task)

        stream_gen = _wrap_chat_stream_with_meta(task, queue_mgr)
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

    chosen = select_instance(proxy, req_obj)
    if not chosen:
        return JSONResponse(
            status_code=503,
            content={"error": "no_instance", "detail": "proxy has no alive instance"},
        )

    host = chosen.host
    port = int(chosen.port)
    url_path = "/v1/completions"

    logger.info("[Proxy] instance chosen(completions): id=%s addr=%s:%s", getattr(chosen, "instance_id", "?"), host, port)
    strategy_name = getattr(proxy.state, "injection_strategy_name", "default")
    if strategy_name == "iws":
        original_mode = getattr(req_obj.Service, "Injection_type", "text")
        try:
            costs = await queue_mgr.estimate_iws_costs(
                req_obj=req_obj,
                instance_id=chosen.instance_id,
                kdn_addr=getattr(req_obj.Task, "KDN_server_addr", None),
            )
            if costs.get("kv_hidden_by_ready_wait"):
                iws_suggest = "kvcache"
            elif (costs.get("kvcache_total_ms") or 0) < (costs.get("text_total_ms") or 0):
                iws_suggest = "kvcache"
            else:
                iws_suggest = "text"
            logger.info(
                "[Proxy][IWS][DryRun] rid=%s original=%s iws_suggest=%s applied=%s ready_wait=%s "
                "kv_prepare=%s kv_hidden=%s text_total=%s kvcache_total=%s "
                "text_service=%s kvcache_service=%s kv_transfer=%s kv_queue_wait=%s "
                "redis_load=%s residual_prefill=%s effective_len=%s residual_tokens=%s "
                "bw=%s bw_src=%s",
                req_obj.Request_ID,
                original_mode,
                iws_suggest,
                original_mode,
                costs.get("ready_wait_ms"),
                costs.get("kvcache_prepare_ms"),
                costs.get("kv_hidden_by_ready_wait"),
                costs.get("text_total_ms"),
                costs.get("kvcache_total_ms"),
                costs.get("text_service_ms"),
                costs.get("kvcache_service_ms"),
                costs.get("kv_transfer_ms"),
                costs.get("kv_queue_wait_ms"),
                costs.get("redis_load_ms"),
                costs.get("residual_prefill_ms"),
                costs.get("effective_knowledge_len"),
                costs.get("residual_tokens"),
                costs.get("bandwidth_mbps"),
                costs.get("bandwidth_source"),
            )
        except Exception as e:
            logger.warning(
                "[Proxy][IWS][DryRun] estimate_iws_costs failed rid=%s err=%s keep=%s",
                req_obj.Request_ID,
                str(e),
                original_mode,
            )

    # ==================================
    # 送入队列enqueue -> drain -> forward
    # ==================================
    try:
        task = ProxyTask(
            request_id=getattr(req_obj, "Request_ID", None),
            req_obj=req_obj,
            instance_body=instance_body,
            instance_id=chosen.instance_id,
            instance_host=chosen.host,
            instance_port=int(chosen.port),
            kdn_addr=getattr(req_obj.Task, "KDN_server_addr", None),
            url_path=url_path,
        )

        await queue_mgr.enqueue_prepare(task)

        content_bytes = b""
        async for chunk in queue_mgr.iter_response(task):
            if chunk:
                content_bytes += chunk

        # completions 是非流式：worker 应该返回一次性 JSON
        if not content_bytes:
            return JSONResponse(
                status_code=502,
                content={"error": "empty_worker_response", "detail": "instance returned empty body"},
            )

        # 尝试按 JSON 解析；解析失败就原样返回文本，便于排查
        try:
            obj = json.loads(content_bytes.decode("utf-8", errors="replace"))
            obj["_cacheroute_meta"] = {"trace": task.trace}
            return JSONResponse(status_code=200, content=obj)
        except Exception:
            return JSONResponse(
                status_code=200,
                content={
                    "raw": content_bytes.decode("utf-8", errors="replace"),
                    "_cacheroute_meta": {"trace": task.trace},
                }
            )

    except Exception as e:
        logger.exception("[Proxy] 调用 Worker(completions) 失败")
        return JSONResponse(
            status_code=502,
            content={"error": "worker_completions_failed", "detail": str(e)},
        )
