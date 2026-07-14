"""
Proxy_v1.py
---------
Example downstream proxy for the Scheduler:

- Asynchronously receives Request payloads (JSON) forwarded by the Scheduler
- Parses key fields (Request_ID, Prompt, Service, Task, etc.) and restores the internal Request structure
- Builds an OpenAI-style HTTP request body from the Request data
- Calls the downstream Instance
    * /v1/chat/completions  -> streaming text/event-stream
    * /v1/completions       -> non-streaming JSON
- Passes Instance responses through to the Scheduler (chat is streaming, completions is one-shot JSON)

Real vLLM / OpenAI / other backend services can be integrated here later.
"""
from __future__ import annotations

import os
import json
import asyncio
import logging
import time
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
IWS_KDN_QUEUE_PENALTY_ALPHA = float(os.environ.get("IWS_KDN_QUEUE_PENALTY_ALPHA", "0.5"))
IWS_DECISION_MARGIN_MS = int(os.environ.get("IWS_DECISION_MARGIN_MS", "100"))
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

    # uvicorn access log (optional, avoids one line per request)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # If asyncio/anyio is also noisy, add:
    # logging.getLogger("asyncio").setLevel(logging.WARNING)
    # logging.getLogger("anyio").setLevel(logging.WARNING)

# ======================= Proxy initialization =======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Proxy lifecycle:
      - startup: register with the scheduler control plane
      - running: send periodic heartbeats so proxy_pool does not expire
      - shutdown: unregister gracefully (not required; TTL handles kill -9 cases)
    """
    _squelch_noisy_loggers()
    app.state.injection_strategy_name = PROXY_INJECTION_STRATEGY  # type: ignore
    logger.info("[Proxy] injection strategy=%s", app.state.injection_strategy_name)
    # --- Initialize the instance pool and inject it into the proxy control plane ---
    ttl_s = int(os.environ.get("PROXY_INSTANCE_TTL_S", config.INSTANCE_ALIVE_TTL_S))
    app.state.instance_pool = InstancePool(ttl_s=ttl_s)  # type: ignore
    p_control_plane.set_pool(app.state.instance_pool)  # type: ignore

    # --- Load the proxy scheduling strategy for the data plane ---
    strategy_name = os.environ.get("PROXY_INSTANCE_STRATEGY", "round_robin")
    try:
        app.state.instance_strategy = build_instance_strategy(strategy_name)  # type: ignore
        logger.info("[Proxy] instance strategy=%s", strategy_name)
    except Exception as e:
        # Strategy initialization failure is fatal because the data plane cannot select an instance
        logger.error("[Proxy] invalid instance strategy=%s err=%s", strategy_name, str(e))
        raise

    # ---Try to start the proxy control plane for interacting with Instances and dynamically refreshing the InstancePool ---
    cp_host = os.environ.get("PROXY_CP_HOST", config.PROXY_CP_HOST)
    cp_port = int(os.environ.get("PROXY_CP_PORT", config.PROXY_CP_PORT))

    cp_config = uvicorn.Config(
        p_control_plane.control_plane,
        host=cp_host,
        port=cp_port,
        log_level="info",
        access_log=False,
        # Important: do not enable reload/workers; keep embedded mode single-process and single-instance
    )
    cp_server = uvicorn.Server(cp_config)
    app.state._cp_server = cp_server  # type: ignore

    async def _run_cp():
        await cp_server.serve()

    app.state._cp_task = asyncio.create_task(_run_cp())  # type: ignore
    logger.info("[Proxy] control plane started: http://%s:%s", cp_host, cp_port)

    # --- Enable the scheduler client to register with the scheduler and keep the scheduler heartbeat alive ---
    client = SchedulerControlClient(SCHEDULER_CP_URL, timeout_s=5.0)
    app.state._sched_client = client  # type: ignore
    app.state._proxy_id = PROXY_ID    # type: ignore
    app.state._hb_stop = asyncio.Event()  # type: ignore

    # --- Heartbeat log aggregation (output layer)---
    app.state._hb_reporter = HeartbeatReporter(interval_s=30.0)  # type: ignore
    app.state._hb_report_task = asyncio.create_task(  # type: ignore
        hb_report_loop(
            reporter=app.state._hb_reporter,  # type: ignore
            logger=logger,
            proxy_id=PROXY_ID,
            stop_event=app.state._hb_stop,  # type: ignore
        )
    )

    # 1) register（failure should not block data-plane startup; allow the proxy to run standalone）
    try:
        # CacheRoute stage 2:
        # Optionally inject static KDN->Proxy topology information for the Scheduler lexicographic strategy.
        # Environment variable example:
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
        # Override the local default heartbeat interval with the interval suggested by the scheduler
        interval = float(reg.heartbeat_interval_s) if reg.heartbeat_interval_s else PROXY_HEARTBEAT_S
        app.state._hb_interval = interval  # type: ignore
        logger.info("[Proxy] registered to scheduler: cp=%s proxy_id=%s advertise=%s:%s hb=%ss",
                    SCHEDULER_CP_URL, reg.proxy_id, PROXY_ADVERTISE_HOST, PROXY_ADVERTISE_PORT, interval)
    except Exception as e:
        # Do not block the data plane: if registration fails, the proxy can still forward locally, but the scheduler cannot see it
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
                # Do not emit a warning for each event to avoid log spam; only record window statistics
                await reporter.record(ok=False, err=str(e))
                # If immediate stack traces are needed, change this to logger.debug(..., exc_info=True)
                logger.debug("[Proxy] heartbeat failed", exc_info=True)

            await asyncio.sleep(float(getattr(app.state, "_hb_interval", PROXY_HEARTBEAT_S)))  # type: ignore

    app.state._hb_task = asyncio.create_task(_hb_loop())  # type: ignore

    try:
        yield
    finally:
        # Stop the control plane
        try:
            srv = getattr(app.state, "_cp_server", None)  # type: ignore
            t = getattr(app.state, "_cp_task", None)  # type: ignore
            if srv is not None:
                srv.should_exit = True
                srv.force_exit = True
            if t is not None:
                # Do not await for long; only give it a short chance to exit
                try:
                    await asyncio.wait_for(t, timeout=2.0)
                except Exception:
                    t.cancel()
        except Exception:
            pass

        # Report to the scheduler
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
queue_mgr = QueueManager()              #create the task queue manager


#--------------------------------------------------------------
# ======================= Common internal helper functions =======================
#--------------------------------------------------------------

def _dataclass_from_dict(dc_cls, data: Dict[str, Any]):
    """
    Safely construct a dataclass from a dict:
      - Only take fields defined by the dataclass to avoid errors from extra fields
      - Missing required fields raise TypeError, which indicates an invalid upstream structure
    """
    if data is None:
        data = {}
    field_names = {f.name for f in fields(dc_cls)}
    filtered = {k: v for k, v in data.items() if k in field_names}
    return dc_cls(**filtered)


def recover_request_from_payload(payload: Dict[str, Any]) -> SchedulerRequest:
    """
        Restore the JSON payload sent by the Scheduler into Request, Prompt, Service, and Task dataclasses.
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
        "[Proxy] restored Request successfully: Request_ID=%s, Endpoint_type=%s, model=%s",
        req_obj.Request_ID,
        getattr(req_obj.Service, "Endpoint_type", None),
        req_obj.Prompt.model,
    )
    return req_obj


def build_body_for_instance(req_obj: SchedulerRequest, mode: str) -> Dict[str, Any]:
    """
        Build the OpenAI-style body sent to the Instance from the Request:
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
        # The Instance chat endpoint follows the OpenAI chat/completions style:
        # messages = [{role: "user", content: "..."}]
        body: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "user", "content": user_prompt},
            ],
            "stream": stream,
        }
    else:
        # completions: prompt plus non-streaming response
        body = {
            "model": model,
            "prompt": user_prompt,
            "stream": False,
        }

        # Add optional parameters when present; omit them otherwise
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p

    return body


def build_cacheroute_meta(task: ProxyTask) -> Dict[str, Any]:
    return {
        "trace": task.trace,
        "kv_ack": task.kv_ack,
        "kv_ready_kids": task.kv_ready_kids,
        "text_only_kids": task.text_only_kids,
        "miss_kids": task.miss_kids,
        "error": task.error,
    }


def _sse_meta_event(task: ProxyTask) -> bytes:
    payload = build_cacheroute_meta(task)
    return (
        "event: cacheroute_meta\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


async def _wrap_chat_stream_with_meta(task: ProxyTask, queue_mgr: QueueManager) -> AsyncGenerator[bytes, None]:
    """
    Forward downstream chat SSE, but delay [DONE] and insert one cacheroute_meta event first.
    """
    pending = b""
    done_seen = False

    try:
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
    except Exception as e:
        task.error = f"stream_wrap_failed: {e}"
        task.trace["stream_exception_ms"] = int(time.time() * 1000)
        logger.exception("[Proxy] stream wrapper failed rid=%s", task.request_id)
    finally:
        if pending:
            yield pending
            pending = b""
        yield _sse_meta_event(task)
        yield b"data: [DONE]\n\n"


def select_instance(app: FastAPI, req_obj: SchedulerRequest):
    """
    Select one instance for the data plane.
    - Input: the current live instance list provided by InstancePool
    - Output: an InstanceInfo with at least host/port/instance_id
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
# ======================= Local proxy route handlers =======================
#--------------------------------------------------------------

@proxy.post("/v1/chat/completions")
async def proxy_chat_completions(request: FastAPIRequest):
    """
    Receive /v1/chat/completions requests from the Scheduler (payload is Request JSON).
    Forward an OpenAI chat/completions body to the worker (streaming).
    """
    proxy_recv_ms = int(time.time() * 1000)
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception as e:
        logger.exception("[Proxy] chat/completions failed to parse JSON")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_json", "detail": str(e)},
        )

    # Restore the internal Request
    try:
        req_obj = recover_request_from_payload(payload)
    except Exception as e:
        logger.exception("[Proxy] failed to restore Request")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request_payload", "detail": str(e)},
        )

    # Build the Instance request body
    instance_body = build_body_for_instance(req_obj, mode="chat")

    route_select_start_ms = int(time.time() * 1000)
    chosen = select_instance(proxy, req_obj)
    route_select_end_ms = int(time.time() * 1000)
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
        applied_mode = original_mode
        try:
            costs = await queue_mgr.estimate_iws_costs(
                req_obj=req_obj,
                instance_id=chosen.instance_id,
                kdn_addr=getattr(req_obj.Task, "KDN_server_addr", None),
            )
            rag_enabled = bool(getattr(req_obj.Service, "Enable_know_injection", False))
            knowledge_len = int(getattr(req_obj.Service, "Knowledge_length", 0) or 0)
            knowledge_list = getattr(req_obj.Service, "Knowledge_List", []) or []
            if (not rag_enabled) or knowledge_len <= 0 or (not knowledge_list):
                iws_suggest = "text"
                iws_reason = "no_rag_or_empty_knowledge"
            else:
                text_total_ms = float(costs.get("text_total_ms") or 0.0)
                kvcache_total_ms = float(costs.get("kvcache_total_ms") or 0.0)
                kv_queue_wait_ms = float(costs.get("kv_queue_wait_ms") or 0.0)
                text_net_wait_ms = float(costs.get("text_net_wait_ms") or 0.0)
                ready_wait_ms = float(costs.get("ready_wait_ms") or 0.0)
                kv_prepare_ms = float(costs.get("kvcache_prepare_ms") or 0.0)
                kdn_queue_penalty_ms = IWS_KDN_QUEUE_PENALTY_ALPHA * kv_queue_wait_ms
                kvcache_score_ms = kvcache_total_ms + kdn_queue_penalty_ms
                text_score_ms = text_total_ms
                costs["kdn_queue_penalty_ms"] = kdn_queue_penalty_ms
                costs["kvcache_score_ms"] = kvcache_score_ms
                costs["text_score_ms"] = text_score_ms
                if kvcache_score_ms + IWS_DECISION_MARGIN_MS < text_score_ms:
                    iws_suggest = "kvcache"
                    if kv_prepare_ms <= ready_wait_ms:
                        iws_reason = "kvcache_hidden_and_score_better"
                    else:
                        iws_reason = "kvcache_score_better"
                else:
                    iws_suggest = "text"
                    if kv_queue_wait_ms > 0:
                        iws_reason = "text_due_to_kdn_congestion"
                    elif text_net_wait_ms > 0:
                        iws_reason = "text_despite_active_kv_wait"
                    else:
                        iws_reason = "text_score_better"
            req_obj.Service.Injection_type = iws_suggest
            applied_mode = iws_suggest
            logger.info(
                "[Proxy][IWS] rid=%s original=%s iws_suggest=%s applied=%s reason=%s ready_wait=%s "
                "text_prepare_wait=%s text_net_wait=%s text_fetch_fixed=%s kdn_active_until=%s "
                "kv_prepare=%s kv_hidden=%s kv_queue_wait=%s text_total=%s kvcache_total=%s "
                "text_overlap_hidden=%s text_total_formula=%s "
                "text_score=%s kvcache_score=%s kdn_queue_penalty=%s iws_alpha=%s iws_margin=%s "
                "text_service=%s kvcache_service=%s kv_transfer=%s redis_load=%s residual_prefill=%s "
                "effective_len=%s residual_tokens=%s bw=%s bw_src=%s",
                req_obj.Request_ID,
                original_mode,
                iws_suggest,
                applied_mode,
                iws_reason,
                costs.get("ready_wait_ms"),
                costs.get("text_prepare_wait_ms"),
                costs.get("text_net_wait_ms"),
                costs.get("text_fetch_fixed_ms"),
                costs.get("kdn_active_until_ms"),
                costs.get("kvcache_prepare_ms"),
                costs.get("kv_hidden_by_ready_wait"),
                costs.get("kv_queue_wait_ms"),
                costs.get("text_total_ms"),
                costs.get("kvcache_total_ms"),
                costs.get("text_overlap_hidden_ms"),
                "overlap",
                costs.get("text_score_ms"),
                costs.get("kvcache_score_ms"),
                costs.get("kdn_queue_penalty_ms"),
                IWS_KDN_QUEUE_PENALTY_ALPHA,
                IWS_DECISION_MARGIN_MS,
                costs.get("text_service_ms"),
                costs.get("kvcache_service_ms"),
                costs.get("kv_transfer_ms"),
                costs.get("redis_load_ms"),
                costs.get("residual_prefill_ms"),
                costs.get("effective_knowledge_len"),
                costs.get("residual_tokens"),
                costs.get("bandwidth_mbps"),
                costs.get("bandwidth_source"),
            )
        except Exception as e:
            logger.warning(
                "[Proxy][IWS] estimate_iws_costs failed rid=%s err=%s keep=%s",
                req_obj.Request_ID,
                str(e),
                original_mode,
            )

    # ====================================
    # Send into the queue: enqueue -> manager -> forward
    # ====================================
    try:
        # 1) Wrap the task (note: chosen comes from RR and has instance_id/host/port fields)
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
        task.trace["proxy_recv_ms"] = proxy_recv_ms
        task.trace["route_select_start_ms"] = route_select_start_ms
        task.trace["route_select_end_ms"] = route_select_end_ms

        await queue_mgr.enqueue_prepare(task)

        stream_gen = _wrap_chat_stream_with_meta(task, queue_mgr)
        return StreamingResponse(stream_gen, media_type="text/event-stream")

    except Exception as e:
        logger.exception("[Proxy] failed to call Worker(chat)")
        return JSONResponse(
            status_code=502,
            content={"error": "worker_chat_failed", "detail": str(e)},
        )



@proxy.post("/v1/completions")
async def proxy_completions(request: FastAPIRequest):
    """
    Receive /v1/completions requests from the Scheduler.
    The demo logic is the same as chat/completions, but leaves room for extension.
    """
    proxy_recv_ms = int(time.time() * 1000)
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception as e:
        logger.exception("[Proxy] completions failed to parse JSON")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_json", "detail": str(e)},
        )

    # Restore the internal Request
    try:
        req_obj = recover_request_from_payload(payload)
    except Exception as e:
        logger.exception("[Proxy] failed to restore Request")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request_payload", "detail": str(e)},
        )

    # Build the Instance request body
    instance_body = build_body_for_instance(req_obj, mode="completions")

    route_select_start_ms = int(time.time() * 1000)
    chosen = select_instance(proxy, req_obj)
    route_select_end_ms = int(time.time() * 1000)
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
        applied_mode = original_mode
        try:
            costs = await queue_mgr.estimate_iws_costs(
                req_obj=req_obj,
                instance_id=chosen.instance_id,
                kdn_addr=getattr(req_obj.Task, "KDN_server_addr", None),
            )
            rag_enabled = bool(getattr(req_obj.Service, "Enable_know_injection", False))
            knowledge_len = int(getattr(req_obj.Service, "Knowledge_length", 0) or 0)
            knowledge_list = getattr(req_obj.Service, "Knowledge_List", []) or []
            if (not rag_enabled) or knowledge_len <= 0 or (not knowledge_list):
                iws_suggest = "text"
                iws_reason = "no_rag_or_empty_knowledge"
            else:
                text_total_ms = float(costs.get("text_total_ms") or 0.0)
                kvcache_total_ms = float(costs.get("kvcache_total_ms") or 0.0)
                kv_queue_wait_ms = float(costs.get("kv_queue_wait_ms") or 0.0)
                text_net_wait_ms = float(costs.get("text_net_wait_ms") or 0.0)
                ready_wait_ms = float(costs.get("ready_wait_ms") or 0.0)
                kv_prepare_ms = float(costs.get("kvcache_prepare_ms") or 0.0)
                kdn_queue_penalty_ms = IWS_KDN_QUEUE_PENALTY_ALPHA * kv_queue_wait_ms
                kvcache_score_ms = kvcache_total_ms + kdn_queue_penalty_ms
                text_score_ms = text_total_ms
                costs["kdn_queue_penalty_ms"] = kdn_queue_penalty_ms
                costs["kvcache_score_ms"] = kvcache_score_ms
                costs["text_score_ms"] = text_score_ms
                if kvcache_score_ms + IWS_DECISION_MARGIN_MS < text_score_ms:
                    iws_suggest = "kvcache"
                    if kv_prepare_ms <= ready_wait_ms:
                        iws_reason = "kvcache_hidden_and_score_better"
                    else:
                        iws_reason = "kvcache_score_better"
                else:
                    iws_suggest = "text"
                    if kv_queue_wait_ms > 0:
                        iws_reason = "text_due_to_kdn_congestion"
                    elif text_net_wait_ms > 0:
                        iws_reason = "text_despite_active_kv_wait"
                    else:
                        iws_reason = "text_score_better"
            req_obj.Service.Injection_type = iws_suggest
            applied_mode = iws_suggest
            logger.info(
                "[Proxy][IWS] rid=%s original=%s iws_suggest=%s applied=%s reason=%s ready_wait=%s "
                "text_prepare_wait=%s text_net_wait=%s text_fetch_fixed=%s kdn_active_until=%s "
                "kv_prepare=%s kv_hidden=%s kv_queue_wait=%s text_total=%s kvcache_total=%s "
                "text_overlap_hidden=%s text_total_formula=%s "
                "text_score=%s kvcache_score=%s kdn_queue_penalty=%s iws_alpha=%s iws_margin=%s "
                "text_service=%s kvcache_service=%s kv_transfer=%s redis_load=%s residual_prefill=%s "
                "effective_len=%s residual_tokens=%s bw=%s bw_src=%s",
                req_obj.Request_ID,
                original_mode,
                iws_suggest,
                applied_mode,
                iws_reason,
                costs.get("ready_wait_ms"),
                costs.get("text_prepare_wait_ms"),
                costs.get("text_net_wait_ms"),
                costs.get("text_fetch_fixed_ms"),
                costs.get("kdn_active_until_ms"),
                costs.get("kvcache_prepare_ms"),
                costs.get("kv_hidden_by_ready_wait"),
                costs.get("kv_queue_wait_ms"),
                costs.get("text_total_ms"),
                costs.get("kvcache_total_ms"),
                costs.get("text_overlap_hidden_ms"),
                "overlap",
                costs.get("text_score_ms"),
                costs.get("kvcache_score_ms"),
                costs.get("kdn_queue_penalty_ms"),
                IWS_KDN_QUEUE_PENALTY_ALPHA,
                IWS_DECISION_MARGIN_MS,
                costs.get("text_service_ms"),
                costs.get("kvcache_service_ms"),
                costs.get("kv_transfer_ms"),
                costs.get("redis_load_ms"),
                costs.get("residual_prefill_ms"),
                costs.get("effective_knowledge_len"),
                costs.get("residual_tokens"),
                costs.get("bandwidth_mbps"),
                costs.get("bandwidth_source"),
            )
        except Exception as e:
            logger.warning(
                "[Proxy][IWS] estimate_iws_costs failed rid=%s err=%s keep=%s",
                req_obj.Request_ID,
                str(e),
                original_mode,
            )

    # ==================================
    # Send into the queue: enqueue -> drain -> forward
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
        task.trace["proxy_recv_ms"] = proxy_recv_ms
        task.trace["route_select_start_ms"] = route_select_start_ms
        task.trace["route_select_end_ms"] = route_select_end_ms

        await queue_mgr.enqueue_prepare(task)

        content_bytes = b""
        async for chunk in queue_mgr.iter_response(task):
            if chunk:
                content_bytes += chunk

        # completions is non-streaming: the worker should return a one-shot JSON response
        if not content_bytes:
            return JSONResponse(
                status_code=502,
                content={"error": "empty_worker_response", "detail": "instance returned empty body"},
            )

        # Try to parse as JSON; if parsing fails, return the raw text for debugging
        try:
            obj = json.loads(content_bytes.decode("utf-8", errors="replace"))
            obj["_cacheroute_meta"] = build_cacheroute_meta(task)
            return JSONResponse(status_code=200, content=obj)
        except Exception:
            return JSONResponse(
                status_code=200,
                content={
                    "raw": content_bytes.decode("utf-8", errors="replace"),
                    "_cacheroute_meta": build_cacheroute_meta(task),
                }
            )

    except Exception as e:
        logger.exception("[Proxy] failed to call Worker(completions)")
        return JSONResponse(
            status_code=502,
            content={"error": "worker_completions_failed", "detail": str(e)},
        )
