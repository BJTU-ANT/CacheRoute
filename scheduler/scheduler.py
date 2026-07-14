"""
Simple FastAPI-based Scheduler HTTP entry point.
Compared with v0 scheduler.py, this uses the HTTP integration library for sending requests instead of protocol/interface.

Scheduler core:
  - Provide HTTP APIs based on FastAPI
  - Start the control plane at startup and build proxy/KDN pools
  - The control plane receives registrations from proxy and KDN servers and maintains resource pools
  - Receive POST requests for /v1/chat/completions and /v1/completions
  - Parse URL / payload / client IP
  - Assign request_id in a 1-65535 cycle
  - Call Request.build_request(...) to build the internal Request object; during construction the strategy selects the best KDN and proxy

Scheduler workflow:
  Scheduling layer:
  - Scheduler receives an OpenAI-style HTTP request;
  - Build the request, including assigning an ID and deciding services plus next-level routing
  - Decide the concrete downstream URL to call.
  Forwarding layer:
  - Take the downstream URL plus outgoing data and headers,
  - use forward_request to send the request to the Proxy,
  - pull the stream from downstream while pushing it back unchanged to the user (StreamingResponse).

Proxy / streaming-transfer logic can be connected here later.
"""

from __future__ import annotations
import sys
import asyncio
import os, logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path

import uvicorn
# import aiohttp
# import httpx
from typing import Any, Dict, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from core import TokenizerRegistry
from core import Request as SchedulerRequest
from core import forward_request
from core import config

from model import EmbeddingEngine
# from .knowledge.kdn_client import fetch_kdn_snapshot

from .knowledge.kdn_sync import (
    # build_table_from_kdn_items,
    kdn_auto_refresh_loop,
    kdn_refresh_once
)
from .resource.control_plane import control_plane, set_pool, get_pool, set_kdn_pool, get_kdn_pool, set_on_kdn_register
from .resource.proxy_pool import ProxyPool
from .resource.kdn_pool import KDNPool
from .strategy import create_strategy

# from store import (
#     # DummyEmbeddingModel,
#     init_knowledge_table,
#     # KnowledgeTable,
#     # KnowledgeUnit
# )

from util import timing

# Use a fallback default when no external value is provided, making local runs easier
from core.config import DEFAULT_MODEL, DEFAULT_EMBED_MODEL, SCHEDULER_CP_PORT


# ======================= Request ID allocator and other basic functions=======================

class RequestIdAllocator:
    """
    Simple 16-bit request ID allocator:
      - valid range: 1 to max_id, default 65535
      - restart from 1 after exceeding the range
    """
    def __init__(self, max_id: int = 65535) -> None:
        self._max_id = max_id
        self._current = 0

    def next_id(self) -> int:
        self._current += 1
        if self._current > self._max_id:
            self._current = 1
        return self._current


id_alloc = RequestIdAllocator()
logger = logging.getLogger("scheduler")
logger.setLevel(logging.INFO)


class PeekPayload(BaseModel):
    kids: List[str]
    need_fields: List[str] = ["length", "avail_kdn_servers", "text_abstract"]


def init_logging() -> str:
    """
    Goals:
    1) SCHEDULER_LOG_FILE may be configured as either a directory path or a file path
       - directory: /logs/scheduler -> /logs/scheduler/scheduler-YYYYMMDD-HHMMSS.log
       - file: /logs/scheduler/scheduler.log -> /logs/scheduler/scheduler-YYYYMMDD-HHMMSS.log
    2) Fix repeated handler creation by reusing an existing RotatingFileHandler first and creating one only if none exists
    3) Isolate uvicorn impact on the root logger: write business logs to an independent logger with propagate=False
    4) Only output ERROR to the console so errors are immediately visible
    """
    # ---------- 1) Parse user configuration: directory or file ----------
    cfg_path = os.environ.get("SCHEDULER_LOG_FILE", getattr(config, "SCHEDULER_LOG_FILE", "scheduler.log"))
    p = Path(str(cfg_path)).expanduser()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    if p.suffix.lower() == ".log":
        # The user provided a file path
        base_dir = p.parent
        stem = p.stem or "scheduler"
    else:
        # The user provided a directory path
        base_dir = p
        stem = "scheduler"

    base_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(base_dir / f"{stem}-{ts}.log")

    # ---------- 2) Business logger, not through root ----------
    biz = logging.getLogger("scheduler")
    cp = logging.getLogger("scheduler.control_plane")
    hb = logging.getLogger("scheduler.hbreport")

    for lg in (biz, cp, hb):
        lg.setLevel(logging.INFO)
        lg.propagate = False

    # ---------- 3) Reuse an existing file handler first (required fix 1) ----------
    def _find_same_file_handler(lg: logging.Logger, target: str) -> RotatingFileHandler | None:
        for h in lg.handlers:
            if isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == target:
                return h
        return None

    fh = (
        _find_same_file_handler(biz, log_path)
        or _find_same_file_handler(cp, log_path)
        or _find_same_file_handler(hb, log_path)
    )

    if fh is None:
        file_fmt = logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s %(name)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh = RotatingFileHandler(
            log_path,
            maxBytes=50 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(logging.INFO)
        fh.setFormatter(file_fmt)

    # Attach to three loggers, avoiding duplicate adds
    for lg in (biz, cp, hb):
        if fh not in lg.handlers:
            lg.addHandler(fh)

    # ---------- 4) root console: ERROR only ----------
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    if not stream_handlers:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.ERROR)
        sh.setFormatter(logging.Formatter("%(levelname)s | %(name)s | %(message)s"))
        root.addHandler(sh)
    else:
        for h in stream_handlers:
            h.setLevel(logging.ERROR)

    # ---------- 5) Noise reduction, optional ----------
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # ---------- 6) Self-check: write one record and flush immediately ----------
    biz.info("[Scheduler] logging initialized, log_path=%s", log_path)
    for h in biz.handlers:
        try:
            h.flush()
        except Exception:
            pass

    return log_path


# ======================= Scheduler initialization =======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifecycle management:
      - startup: warm up the tokenizer
      - shutdown: no resources to clean up currently; interface reserved
    """
    log_path = init_logging()
    print(f"[Scheduler] started. log_file={log_path}")
    # ------------------------------------------
    # Try to warm up the tokenizer
    # ------------------------------------------
    model_path = os.getenv("SCHEDULER_MODEL_PATH", DEFAULT_MODEL)
    try:
        # print("[Scheduler] startup: start warming up tokenizer")
        TokenizerRegistry.warmup_tokenizers(model_path)
        logger.info(f"[Scheduler] Warmup tokenizers, model_path={model_path!r}")
    except Exception as e:
        logger.info(f"[Scheduler] tokenizer warmup failed:{e}")

    # ------------------------------------------
    # Try to warm up the embedding model
    # ------------------------------------------
    embedding_model_name = os.getenv("SCHEDULER_EMBEDDING_MODEL", DEFAULT_EMBED_MODEL)
    if os.path.isabs(embedding_model_name) and not os.path.isdir(embedding_model_name):
        raise RuntimeError(
            f"SCHEDULER_EMBEDDING_MODEL is a local path but not found: {embedding_model_name}"
        )

    try:
        embedder = EmbeddingEngine(model_name=embedding_model_name)
        app.state.embedding_engine = embedder # type: ignore

        try:
            _ = embedder.encode_vector(["__warm_up__"])[0]
            logger.info(
                f"[Scheduler] Warmup embedding model: {embedding_model_name!r}, "
                f"dim={embedder.dim}, device={embedder.device}"
            )

        except Exception as e:
            logger.warning(f"[Scheduler] Warmup embedding model failed: {e}")

    except Exception as e:
        logger.exception(f"[Scheduler] embedding model warmup failed: {e}")
        app.state.embedding_engine = None  # type: ignore

    # -------------------------------------------------------------
    # Try to start the control plane, enable proxy/KDN pools, and pull the knowledge list from the KDN pool to initialize the knowledge base
    # -------------------------------------------------------------
    #Build the ProxyPool instance first
    ttl = int(os.environ.get("SCHEDULER_PROXY_TTL_S", config.CONTROL_PLANE_TTL_S))
    app.state.proxy_pool = ProxyPool(ttl_s=ttl)  # type: ignore
    set_pool(app.state.proxy_pool)  # type: ignore let 7002 reuse this pool

    kdn_ttl = int(os.environ.get("SCHEDULER_KDN_TTL_S", config.CONTROL_PLANE_TTL_S))
    app.state.kdn_pool = KDNPool(ttl_s=kdn_ttl)  # type: ignore
    set_kdn_pool(app.state.kdn_pool)  # type: ignore

    app.state.knowledge_table = None  # type: ignore
    app.state.last_refresh_ts = 0  # type: ignore

    # KDN refresh loop infra (always on)
    app.state._kdn_refresh_lock = asyncio.Lock()  # type: ignore
    app.state._kdn_stop_event = asyncio.Event()  # type: ignore
    app.state._kdn_refresh_task = asyncio.create_task(  # type: ignore
        kdn_auto_refresh_loop(app, app.state._kdn_stop_event)  # type: ignore
    )

    logger.info("[Scheduler] KDN refresh loop started (pool-based).")

    async def _trigger_refresh_once() -> None:
        # If refresh is already running, skip directly and reuse the existing lock
        lock = getattr(app.state, "_kdn_refresh_lock", None) # type: ignore
        if lock is None:
            return
        if lock.locked():
            return
        try:
            await kdn_refresh_once(app)
        except Exception:
            logger.exception("[Scheduler] kdn_refresh_once failed when triggered by kdn_register")

    set_on_kdn_register(_trigger_refresh_once)
    logger.info("[Scheduler] on_kdn_register callback installed")

    async def _run_control_plane(app):
        host = os.environ.get("SCHEDULER_CP_HOST", "0.0.0.0")
        port = int(os.environ.get("SCHEDULER_CP_PORT", SCHEDULER_CP_PORT))

        uv_cfg = uvicorn.Config(
            control_plane,
            host=host,
            port=port,
            log_level=os.environ.get("SCHEDULER_CP_LOG_LEVEL", "info"),
            loop="asyncio",
            lifespan="on",
            access_log=False,
        )
        server = uvicorn.Server(uv_cfg)
        app.state._cp_server = server
        await server.serve()

    app.state._cp_task = asyncio.create_task(_run_control_plane(app)) # type: ignore

    # ------------------------------------------
    # Call the concrete scheduler strategy
    # ------------------------------------------
    strategy_name = os.environ.get("SCHEDULER_STRATEGY", config.SCHEDULER_DEFAULT_STRATEGY)
    app.state.proxy_strategy = create_strategy(strategy_name)  # type: ignore
    loaded_name = getattr(app.state.proxy_strategy, "name", strategy_name)
    logger.info("[Scheduler] strategy loaded: %s", loaded_name)

    # After this yield, the service is in normal serving period
    logger.info("[Scheduler] startup: initialization complete; service is listening")
    try:
        yield

    finally:
        # ---------- shutdown ----------
        cp_server = getattr(app.state, "_cp_server", None) # type: ignore
        cp_task = getattr(app.state, "_cp_task", None) # type: ignore
        ev = getattr(app.state, "_kdn_stop_event", None) # type: ignore
        task = getattr(app.state, "_kdn_refresh_task", None) # type: ignore

        if ev is not None:
            ev.set()
        if task is not None:
            task.cancel()

        if cp_server is not None:
            cp_server.should_exit = True
        if cp_task is not None:
            try:
                await cp_task
            except Exception:
                pass

    # During shutdown, add resource cleanup here if needed later
    logger.info("[Scheduler] shutdown: service stopped")


scheduler = FastAPI(
    title="CacheRoute Scheduler",
    version="0.1.1",
    lifespan=lifespan,
)




# ======================= Common internal helper functions =======================
@timing
def _handle_client(
    app: FastAPI,
    url_path: str,
    payload: Dict[str, Any],
    client_ip: str,
    proxies: List[Dict[str, Any]] | None = None,
    kdns: List[Dict[str, Any]] | None = None,
    strategy: Any | None = None,
    kdn_knowledge_index: Dict[str, Dict[str, Dict[str, Any]]] | None = None,
) -> SchedulerRequest:
    """
    Based on HTTP request information
    build the internal Request object and assign request_id
      - url_path: for example "/v1/chat/completions"
      - payload: parsed JSON dict
      - client_ip: client IP string
    """
    # Assign request_id in a 1-65535 cycle
    request_id = id_alloc.next_id()

    # Print debug information
    if os.environ.get("SCHEDULER_VERBOSE_REQUEST_LOG", config.SCHEDULER_VERBOSE_REQUEST_LOG) == 1:
        print("+" * 80)
        print(f"[Scheduler] received HTTP request: path={url_path}, client_ip={client_ip},\n"
              f"assigned request_id={request_id},\n"
              f"payload={payload}")
        print("+" * 80)

    # Directly reuse the updated build_request
    req_obj = SchedulerRequest.build_request(
        url_path=url_path,
        payload=payload,
        user_addr=client_ip,
        request_id=request_id,
        embedder=getattr(app.state, "embedding_engine", None), # type: ignore
        knowledge_table=getattr(app.state, "knowledge_table", None), # type: ignore
        proxies=proxies,
        kdns=kdns,
        strategy=strategy,
        kdn_knowledge_index=kdn_knowledge_index,
    )
    if os.environ.get("SCHEDULER_VERBOSE_REQUEST_LOG", config.SCHEDULER_VERBOSE_REQUEST_LOG) == 1:
        print(f"[Scheduler] built internal Request successfully: Request_ID={req_obj.Request_ID},\n"
              f"[Scheduler] Endpoint_type={getattr(req_obj.Service, 'Endpoint_type', None)},\n"
              f"[Scheduler] Knowledge_List={req_obj.Service.Knowledge_List},\n"
              f"[Scheduler] Selected KDN={req_obj.Task.KDN_server_addr}, ProxyID={req_obj.Task.P_proxy_id}[{req_obj.Task.P_proxy_addr}:{req_obj.Task.P_proxy_port}]"
              )
    print(f"[Scheduler] Injection_type={getattr(req_obj.Service, 'Injection_type', None)}")
    return req_obj


# ======================= Scheduler route handlers =======================
@scheduler.post("/v1/chat/completions")
async def create_chat_completions(request: FastAPIRequest):
    """
    Corresponds to:
        curl http://HOST:PORT/v1/chat/completions -H "Content-Type: application/json" -d '{...}'
        payload structure follows the OpenAI chat API:
          {
            "model": "...",
            "messages": [ ... ],
            "max_tokens": ...,
            "temperature": ...,
            "top_p": ...,
            "stream": true/false,
            ...
          }
    """
    # Parse the user raw request body
    try:
        payload = await request.json()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_json", "detail": str(e)},
        )

    # Get client IP and path for building the internal Request
    client_ip = request.client.host if request.client else "unknown"
    url_path = request.url.path

    # Convert user request headers to dict[str, str]
    raw_headers = {k.lower(): v for k, v in request.headers.items()}

    # Build the internal Request, including Prompt/Service/Task, etc.
    pool = get_pool()
    proxy_infos = await pool.list(include_dead=False)
    proxies = [
        {
            "proxy_id": p.proxy_id,
            "host": p.host,
            "port": p.port,
            "inflight": p.load.inflight,
            "max_inflight": p.load.max_capacity,
            "qps_1m": p.load.qps_1m,
            "gpu_util": p.load.gpu_util,
            "meta": dict(p.meta or {}),
        }
        for p in proxy_infos
    ]

    kdn_pool = get_kdn_pool()
    kdn_infos = await kdn_pool.list(include_dead=False)
    kdns = [
        {
            "kdn_id": k.kdn_id,
            "host": k.host,
            "port": k.port,
            "items": k.load.items,
            "qps_1m": k.load.qps_1m,
            "pending_transfers": k.load.pending_transfers,
            "active_transfers": k.load.active_transfers,
            "network_queue_ms_ema": k.load.network_queue_ms_ema,
            "meta": dict(k.meta or {}),
        }
        for k in kdn_infos
    ]

    strategy = getattr(request.app.state, "proxy_strategy", None)
    req_obj = _handle_client(
        request.app,
        url_path,
        payload,
        client_ip,
        proxies=proxies,
        kdns=kdns,
        strategy=strategy,
        kdn_knowledge_index=getattr(request.app.state, "kdn_knowledge_index", {}),
    )

    if not req_obj.Task.KDN_server_addr or not req_obj.Task.P_proxy_addr or req_obj.Task.P_proxy_port <= 0:
        raise RuntimeError("Routing failed: missing KDN or Proxy selection")

    # Select downstream URL from the scheduling result and update inflight for the corresponding proxy_id
    host = req_obj.Task.P_proxy_addr
    port = req_obj.Task.P_proxy_port
    endpoint = getattr(req_obj.Service, "Endpoint_type", "chat/completions")
    downstream_url = f"http://{host}:{port}/v1/{endpoint}"
    proxy_id = req_obj.Task.P_proxy_id
    if not proxy_id:
        raise RuntimeError("Routing failed: missing P_proxy_id for inflight tracking")
    ok = await pool.inflight_delta(proxy_id, +1)
    if not ok:
        raise RuntimeError(f"Proxy not found in pool: {proxy_id}")

    # Transform payload
    data_for_downstream = req_obj.to_payload()

    # Handle headers that need to be passed through downstream, optionally filtering them
    extra_headers = {}

    # Pass the user Authorization downstream unchanged if Proxy should perform authentication
    if "authorization" in raw_headers:
        extra_headers["authorization"] = raw_headers["authorization"]

    # Carry a Scheduler-assigned Request ID for traceability
    extra_headers["scheduler-request-id"] = str(req_obj.Request_ID)

    # Define an async generator that reads streaming data from downstream and returns it upstream
    async def iter_upstream():
        try:
            # forward_request itself is an async generator
            async for chunk in forward_request(
                url=downstream_url,
                data=data_for_downstream,
                use_chunked=True,            # chat/completions is usually streaming
                extra_headers=extra_headers, # pass through required headers
            ):
                # chunk is already bytes here, so yield it directly
                yield chunk
        finally:
            await pool.inflight_delta(proxy_id, -1)

    # Wrap with StreamingResponse to provide the user-side streaming response
    return StreamingResponse(iter_upstream(), media_type="application/json")


@scheduler.post("/v1/completions")
async def create_completions(request: FastAPIRequest):
    """
    Corresponds to:
      curl http://HOST:PORT/v1/completions -H "Content-Type: application/json" -d '{...}'
    payload structure is similar to the OpenAI completions API:
      {
        "model": "...",
        "prompt": "...." or ["..."],
        "max_tokens": ...,
        "temperature": ...,
        "top_p": ...,
        "stream": true/false,
        ...
      }
    """
    try:
        payload = await request.json()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_json", "detail": str(e)},
        )

    # Get client IP and path for building the internal Request
    client_ip = request.client.host if request.client else "unknown"
    url_path = request.url.path

    # Convert user request headers to dict[str, str]
    raw_headers = {k.lower(): v for k, v in request.headers.items()}

    # Build the internal Request, including Prompt/Service/Task, etc.
    pool = get_pool()
    proxy_infos = await pool.list(include_dead=False)
    proxies = [
        {
            "proxy_id": p.proxy_id,
            "host": p.host,
            "port": p.port,
            "inflight": p.load.inflight,
            "max_inflight": p.load.max_capacity,
            "qps_1m": p.load.qps_1m,
            "gpu_util": p.load.gpu_util,
            "meta": dict(p.meta or {}),
        }
        for p in proxy_infos
    ]

    kdn_pool = get_kdn_pool()
    kdn_infos = await kdn_pool.list(include_dead=False)
    kdns = [
        {
            "kdn_id": k.kdn_id,
            "host": k.host,
            "port": k.port,
            "items": k.load.items,
            "qps_1m": k.load.qps_1m,
            "pending_transfers": k.load.pending_transfers,
            "active_transfers": k.load.active_transfers,
            "network_queue_ms_ema": k.load.network_queue_ms_ema,
            "meta": dict(k.meta or {}),
        }
        for k in kdn_infos
    ]

    strategy = getattr(request.app.state, "proxy_strategy", None)
    req_obj = _handle_client(
        request.app,
        url_path,
        payload,
        client_ip,
        proxies=proxies,
        kdns=kdns,
        strategy=strategy,
        kdn_knowledge_index=getattr(request.app.state, "kdn_knowledge_index", {}),
    )

    if not req_obj.Task.KDN_server_addr or not req_obj.Task.P_proxy_addr or req_obj.Task.P_proxy_port <= 0:
        raise RuntimeError("Routing failed: missing KDN or Proxy selection")

    # Select downstream URL from the scheduling result and update inflight for the corresponding proxy_id
    host = req_obj.Task.P_proxy_addr
    port = req_obj.Task.P_proxy_port
    endpoint = getattr(req_obj.Service, "Endpoint_type", "completions")
    downstream_url = f"http://{host}:{port}/v1/{endpoint}"
    proxy_id = req_obj.Task.P_proxy_id
    if not proxy_id:
        raise RuntimeError("Routing failed: missing P_proxy_id for inflight tracking")
    ok = await pool.inflight_delta(proxy_id, +1)
    if not ok:
        raise RuntimeError(f"Proxy not found in pool: {proxy_id}")

    # Transform payload
    data_for_downstream = req_obj.to_payload()

    # Handle headers that need to be passed through downstream, optionally filtering them
    extra_headers = {}

    # Pass the user Authorization downstream unchanged if Proxy should perform authentication
    if "authorization" in raw_headers:
        extra_headers["authorization"] = raw_headers["authorization"]

    # Carry a Scheduler-assigned Request ID for traceability
    extra_headers["scheduler-request-id"] = str(req_obj.Request_ID)

    # Define an async generator that reads streaming data from downstream and returns it upstream
    async def iter_upstream():
        try:
            # forward_request itself is an async generator
            async for content in forward_request(
                    url=downstream_url,
                    data=data_for_downstream,
                    use_chunked=False,  # chat/completions is usually streaming
                    extra_headers=extra_headers,  # pass through required headers
            ):
                # chunk is already bytes here, so yield it directly
                yield content
        finally:
            await pool.inflight_delta(proxy_id, -1)

    # Wrap with StreamingResponse to provide the user-side streaming response
    return StreamingResponse(iter_upstream(), media_type="application/json")


@scheduler.get("/debug/status")
async def debug_status() -> Dict[str, Any]:
    """
    Debug endpoint for Scheduler CLI.

    Returns:
      - knowledge_loaded: whether knowledge_table is ready
      - entries: number of knowledge units
      - dim: embedding dim
      - faiss_total: index.ntotal if built (else None)
      - kdn_base_url: where snapshot comes from (if any)
      - last_refresh_ts: unix seconds (if you maintain it), else None
      - unit_fields: what fields a KnowledgeUnit contains (for inspection)
      - sample_kids: first few kids (for quick view)
    """
    table = getattr(scheduler.state, "knowledge_table", None)   # type: ignore
    kdn_base_url = getattr(scheduler.state, "kdn_base_url", None)   # type: ignore
    last_refresh_ts = getattr(scheduler.state, "last_refresh_ts", None) # type: ignore
    kdn_alive = int(getattr(scheduler.state, "kdn_alive", 0) or 0) # type: ignore
    kdn_alive_addrs = list(getattr(scheduler.state, "kdn_alive_addrs", []) or []) # type: ignore
    kdn_last_selected = getattr(scheduler.state, "kdn_last_selected", None) # type: ignore
    kdn_last_selected_id = getattr(scheduler.state, "kdn_last_selected_id", None) # type: ignore
    kdn_last_refresh_ok = bool(getattr(scheduler.state, "kdn_last_refresh_ok", False)) # type: ignore
    kdn_last_refresh_reason = getattr(scheduler.state, "kdn_last_refresh_reason", "") # type: ignore
    kdn_last_refresh_ts = int(getattr(scheduler.state, "kdn_last_refresh_ts", 0) or 0) # type: ignore

    pool = get_pool()
    proxy_infos = await pool.list(include_dead=False)
    kdn_pool = get_kdn_pool()
    kdn_infos = await kdn_pool.list(include_dead=False)
    strategy = getattr(scheduler.state, "proxy_strategy", None)  # type: ignore
    strategy_name = getattr(strategy, "name", None) or (type(strategy).__name__ if strategy else None)

    proxy_states = []
    for p in proxy_infos:
        proxy_states.append({
            "proxy_id": p.proxy_id,
            "host": p.host,
            "port": p.port,
            "max_capacity": p.load.max_capacity,
            "instance_count": p.load.instance_count,
            "kv_mem_per_instance_gb": p.load.kv_mem_per_instance_gb,
            "kv_cache_pool_gb": p.load.kv_cache_pool_gb,
            "kv_cache_update_policy": getattr(p, "kv_cache_update_policy", "static"),
            "inflight": p.load.inflight,
            "qps_1m": p.load.qps_1m,
            "gpu_util": p.load.gpu_util,
        })
    kdn_states = []
    for k in kdn_infos:
        kdn_states.append({
            "kdn_id": k.kdn_id,
            "host": k.host,
            "port": k.port,
            "items": k.load.items,
            "qps_1m": k.load.qps_1m,
            "pending_transfers": k.load.pending_transfers,
            "active_transfers": k.load.active_transfers,
            "network_queue_ms_ema": k.load.network_queue_ms_ema,
        })

    if table is None:

        return {
            "strategy": strategy_name,
            "knowledge_loaded": False,
            "entries": 0,
            "dim": None,
            "faiss_total": None,
            "kdn_base_url": kdn_base_url,
            "last_refresh_ts": last_refresh_ts,
            "unit_fields": [],
            "sample_kids": [],
            "kdn_alive": 0,
            "kdn_alive_addrs": [],
            "kdn_last_selected": None,
            "kdn_last_selected_id": None,
            "kdn_last_refresh_ok": False,
            "kdn_last_refresh_reason": "",
            "kdn_last_refresh_ts": 0,
            "kdns": kdn_states,
            "proxies": proxy_states,
        }

    units = getattr(table, "_units", {})
    kids = list(units.keys())
    kids_sorted = sorted(kids)[:10]

    # Try to get dim from the table
    dim = getattr(table, "dim", None)

    # FAISS
    faiss_total = None
    idx = getattr(table, "_faiss_index", None)
    if idx is not None:
        try:
            faiss_total = int(getattr(idx, "ntotal"))
        except Exception:
            faiss_total = None

    # KnowledgeUnit field probing, to help inspect what is actually stored after snapshot
    unit_fields: List[str] = []
    if kids_sorted:
        u0 = units[kids_sorted[0]]
        # dataclasses generally have __dict__
        if hasattr(u0, "__dict__"):
            unit_fields = sorted(list(u0.__dict__.keys()))
        else:
            # fallback：dir filtering
            unit_fields = [x for x in dir(u0) if not x.startswith("_")]

    return {
        "strategy": strategy_name,
        "knowledge_loaded": True,
        "entries": len(units),
        "dim": dim,
        "faiss_total": faiss_total,
        "kdn_base_url": kdn_base_url,
        "last_refresh_ts": last_refresh_ts,
        "unit_fields": unit_fields,
        "sample_kids": kids_sorted,
        "kdn_alive": kdn_alive,
        "kdn_alive_addrs": kdn_alive_addrs,
        "kdn_last_selected": kdn_last_selected,
        "kdn_last_selected_id": kdn_last_selected_id,
        "kdn_last_refresh_ok": kdn_last_refresh_ok,
        "kdn_last_refresh_reason": kdn_last_refresh_reason,
        "kdn_last_refresh_ts": kdn_last_refresh_ts,
        "kdns": kdn_states,
        "proxies": proxy_states,
    }


@scheduler.post("/debug/knowledge/peek")
async def debug_peek_knowledge(payload: PeekPayload) -> Dict[str, Any]:
    """
    Peek knowledge units by kids.
    Default fields are safe (no full embedding).
    """
    table = getattr(scheduler.state, "knowledge_table", None)   # type: ignore
    if table is None:
        return {"items": [], "miss": payload.kids}

    units = getattr(table, "_units", {})
    items = []
    miss = []

    for kid in payload.kids:
        k = (kid or "").strip().lower()
        u = units.get(k)
        if u is None:
            miss.append(kid)
            continue

        it = {"kid": k}
        allow = set(payload.need_fields or [])

        if "length" in allow:
            it["length"] = getattr(u, "length", None)
        if "avail_kdn_servers" in allow:
            it["avail_kdn_servers"] = getattr(u, "avail_kdn_servers", None)
        if "text_abstract" in allow:
            it["text_abstract"] = getattr(u, "text_abstract", None)

        # Optional: if kv_ready or similar fields are stored in KnowledgeUnit/metadata, they can also be output here
        if "meta" in allow and hasattr(u, "meta"):
            it["meta"] = getattr(u, "meta")
        if "kv_ready" in allow:
            it["kv_ready"] = getattr(u, "kv_ready", 0)
        if "kv_rel_dir" in allow:
            it["kv_rel_dir"] = getattr(u, "kv_rel_dir", None)
        if "kv_dumped_keys" in allow:
            it["kv_dumped_keys"] = getattr(u, "kv_dumped_keys", None)
        if "kv_updated_at" in allow:
            it["kv_updated_at"] = getattr(u, "kv_updated_at", None)

        items.append(it)

    return {"items": items, "miss": miss}


@scheduler.post("/admin/refresh_knowledge")
async def admin_refresh_knowledge():
    try:
        r = await kdn_refresh_once(scheduler)
        return JSONResponse(content=r)
    except Exception as e:
        logger.exception(f"[Scheduler] admin refresh failed: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@scheduler.get("/debug/strategy")
async def debug_strategy() -> Dict[str, Any]:
    strategy = getattr(scheduler.state, "proxy_strategy", None)  # type: ignore
    pool = get_pool()
    proxy_infos = await pool.list(include_dead=False)

    # Return only concise information to avoid overly large output
    sample = [{
        "proxy_id": p.proxy_id,
        "host": p.host,
        "port": p.port,
        "is_alive": True,  # list(include_dead=False) already guarantees alive
    } for p in proxy_infos[:10]]

    out = {
        "strategy": getattr(strategy, "name", None) or type(strategy).__name__ if strategy else None,
        "proxy_count": len(proxy_infos),
        "proxies_sample": sample,
    }
    if strategy is not None and hasattr(strategy, "get_debug_snapshot"):
        try:
            out["strategy_debug"] = strategy.get_debug_snapshot()  # type: ignore
        except Exception:
            out["strategy_debug"] = {"error": "strategy debug snapshot unavailable"}
    return out

# Reserved: add routes such as /knowledge/update later
# @api.post("/knowledge/update")
# async def knowledge_update(request: FastAPIRequest):
#     payload = await request.json()
#     client_ip = request.client.host if request.client else "unknown"
#     ...
#     return JSONResponse(content={"status": "ok"})
