"""
基于 FastAPI 的简单 Scheduler HTTP 入口。
相较于v0 scheduler.py，采用http集成库进行发包，不再采用protocol/interface

调度器核心：
  - 基于 FastAPI 提供 HTTP 接口
  - 启动时挂起控制平面，构建proxy池和kdn池
  - 控制平面接收来自proxy和kdn服务器的注册，并维护资源池
  - 接收 /v1/chat/completions 和 /v1/completions 的 POST 请求
  - 解析 URL / payload / 客户端 IP
  - 分配 1~65535 循环的 request_id
  - 调用 Request.build_request(...) 构建内部 Request 对象，构建Request时调用策略选择最优kdn和proxy

调度器Workflow：
  调度层：
  - Scheduler 收到 HTTP 请求（OpenAI 风格）；
  - 构建request（包含分配ID，决定各项服务以及下一级调度）
  - 决定具体要打哪个下游 URL。
  转发层：
  - 拿着“下游 URL + 要发的数据 + headers”，
  - 用 forward_request 把请求送到 Proxy，
  - 一边从下游拉流，一边原样推回给用户（StreamingResponse）。

后续可以在这里接 Proxy / 流式传输等逻辑。
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

# 如果外部没给，就用一个保底默认值（方便本地直接跑）
from core.config import DEFAULT_MODEL, DEFAULT_EMBED_MODEL, SCHEDULER_CP_PORT


# ======================= 请求ID分配器等基本函数=======================

class RequestIdAllocator:
    """
    简单的 16bit 请求 ID 分配器：
      - 有效范围：1 ~ max_id（默认 65535）
      - 超过后从 1 重新开始
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
    目标：
    1) SCHEDULER_LOG_FILE 允许配置为“目录路径”或“文件路径”
       - 目录：/logs/scheduler  -> /logs/scheduler/scheduler-YYYYMMDD-HHMMSS.log
       - 文件：/logs/scheduler/scheduler.log -> /logs/scheduler/scheduler-YYYYMMDD-HHMMSS.log
    2) 修复 handler 重复创建（先复用已有 RotatingFileHandler，找不到才创建）
    3) 隔离 uvicorn 对 root logger 的影响：业务日志写在独立 logger 上 propagate=False
    4) 控制台只输出 ERROR（错误立刻可见）
    """
    # ---------- 1) 解析用户配置：目录 or 文件 ----------
    cfg_path = os.environ.get("SCHEDULER_LOG_FILE", getattr(config, "SCHEDULER_LOG_FILE", "scheduler.log"))
    p = Path(str(cfg_path)).expanduser()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    if p.suffix.lower() == ".log":
        # 用户给的是文件路径
        base_dir = p.parent
        stem = p.stem or "scheduler"
    else:
        # 用户给的是目录路径
        base_dir = p
        stem = "scheduler"

    base_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(base_dir / f"{stem}-{ts}.log")

    # ---------- 2) 业务 logger（不走 root） ----------
    biz = logging.getLogger("scheduler")
    cp = logging.getLogger("scheduler.control_plane")
    hb = logging.getLogger("scheduler.hbreport")

    for lg in (biz, cp, hb):
        lg.setLevel(logging.INFO)
        lg.propagate = False

    # ---------- 3) 先复用已有 file handler（修复必修问题1） ----------
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

    # 挂载到三个 logger（避免重复 add）
    for lg in (biz, cp, hb):
        if fh not in lg.handlers:
            lg.addHandler(fh)

    # ---------- 4) root 控制台：ERROR only ----------
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

    # ---------- 5) 降噪（可选） ----------
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # ---------- 6) 自检：写一条，立刻 flush ----------
    biz.info("[Scheduler] logging initialized, log_path=%s", log_path)
    for h in biz.handlers:
        try:
            h.flush()
        except Exception:
            pass

    return log_path


# ======================= Scheduler初始化 =======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 生命周期管理：
      - startup: 预热分词器
      - shutdown: 目前没资源要清理，预留接口
    """
    log_path = init_logging()
    print(f"[Scheduler] started. log_file={log_path}")
    # ------------------------------------------
    # 尝试预热 tokenizer 分词器
    # ------------------------------------------
    model_path = os.getenv("SCHEDULER_MODEL_PATH", DEFAULT_MODEL)
    try:
        # print("[Scheduler] startup: 开始预热分词器")
        TokenizerRegistry.warmup_tokenizers(model_path)
        logger.info(f"[Scheduler] Warmup tokenizers, model_path={model_path!r}")
    except Exception as e:
        logger.info(f"[Scheduler] 分词器预热失败:{e}")

    # ------------------------------------------
    # 尝试预热Embedding模型
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
        logger.exception(f"[Scheduler] 预热 Embedding 模型失败: {e}")
        app.state.embedding_engine = None  # type: ignore

    # -------------------------------------------------------------
    # 尝试启动控制平面，启用proxy池和KDN池，并从KDN池拉取知识清单来初始化知识库
    # -------------------------------------------------------------
    #先构建Proxy池实例
    ttl = int(os.environ.get("SCHEDULER_PROXY_TTL_S", config.CONTROL_PLANE_TTL_S))
    app.state.proxy_pool = ProxyPool(ttl_s=ttl)  # type: ignore
    set_pool(app.state.proxy_pool)  # type: ignore 让 7002 复用这份池

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
        # 如果 refresh 正在跑，就直接跳过（复用你的锁）
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
    # 调用具体scheduler策略
    # ------------------------------------------
    strategy_name = os.environ.get("SCHEDULER_STRATEGY", "round_robin")
    app.state.proxy_strategy = create_strategy(strategy_name)  # type: ignore
    logger.info("[Scheduler] scheduler strategy=%s", strategy_name)

    # 这里 yield 之后是正常服务期
    logger.info("[Scheduler] startup: 初始化完成，监听服务中")
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

    # shutdown 阶段，如果后面有资源要清理，可以写在这里
    logger.info("[Scheduler] shutdown: 结束服务")


scheduler = FastAPI(
    title="CacheRoute Scheduler",
    version="0.1.1",
    lifespan=lifespan,
)




# ======================= 公共内部处理函数 =======================
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
    根据 HTTP 请求信息
    构造内部 Request 对象，分配request_id
      - url_path: 例如 "/v1/chat/completions"
      - payload: 解析后的 JSON 字典
      - client_ip: 客户端 IP 字符串
    """
    # 分配 request_id（1~65535 循环）
    request_id = id_alloc.next_id()

    # 打印调试信息
    if os.environ.get("SCHEDULER_VERBOSE_REQUEST_LOG", config.SCHEDULER_VERBOSE_REQUEST_LOG) == 1:
        print("+" * 80)
        print(f"[Scheduler] 收到 HTTP 请求: path={url_path}, client_ip={client_ip},\n"
              f"分配 request_id={request_id},\n"
              f"payload={payload}")
        print("+" * 80)

    # 直接复用你改好的 build_request
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
        print(f"[Scheduler] 构建内部 Request 成功: Request_ID={req_obj.Request_ID},\n"
              f"[Scheduler] Endpoint_type={getattr(req_obj.Service, 'Endpoint_type', None)},\n"
              f"[Scheduler] Knowledge_List={req_obj.Service.Knowledge_List},\n"
              f"[Scheduler] Selected KDN={req_obj.Task.KDN_server_addr}, ProxyID={req_obj.Task.P_proxy_id}[{req_obj.Task.P_proxy_addr}:{req_obj.Task.P_proxy_port}]"
              )
    print(f"[Scheduler] Injection_type={getattr(req_obj.Service, 'Injection_type', None)}")
    return req_obj


# ======================= 调度器方法路由 =======================
@scheduler.post("/v1/chat/completions")
async def create_chat_completions(request: FastAPIRequest):
    """
    对应：
        curl http://HOST:PORT/v1/chat/completions -H "Content-Type: application/json" -d '{...}'
        payload 结构参考 OpenAI chat 接口：
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
    # 解析用户原始请求体
    try:
        payload = await request.json()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_json", "detail": str(e)},
        )

    # 取客户端 IP 和路径（用于构建内部 Request）
    client_ip = request.client.host if request.client else "unknown"
    url_path = request.url.path

    # 把用户请求头转成 dict[str, str]
    raw_headers = {k.lower(): v for k, v in request.headers.items()}

    # 构建内部 Request（包含 Prompt/Service/Task 等）
    pool = get_pool()
    proxy_infos = await pool.list(include_dead=False)
    proxies = [
        {
            "proxy_id": p.proxy_id,
            "host": p.host,
            "port": p.port,
            "inflight": p.load.inflight,
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

    # 根据调度结果选择下游 URL，更新对应proxy_id的inflight
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

    # 转化payload
    data_for_downstream = req_obj.to_payload()

    # 处理需要透传给下游的 headers（可选择性过滤）
    extra_headers = {}

    # 把用户的 Authorization 原样传给下游（如果希望由 Proxy 做鉴权）
    if "authorization" in raw_headers:
        extra_headers["authorization"] = raw_headers["authorization"]

    # 带一个 Scheduler分配的Request ID，便于链路追踪
    extra_headers["scheduler-request-id"] = str(req_obj.Request_ID)

    # 定义一个 async 生成器，从下游流式读取，再回给上游
    async def iter_upstream():
        try:
            # forward_request 本身是一个 async generator
            async for chunk in forward_request(
                url=downstream_url,
                data=data_for_downstream,
                use_chunked=True,            # chat/completions 一般是流式
                extra_headers=extra_headers, # 透传必要头
            ):
                # 这里 chunk 已经是 bytes，直接 yield 出去即可
                yield chunk
        finally:
            await pool.inflight_delta(proxy_id, -1)

    # 用 StreamingResponse 包一层，实现用户侧的流式响应
    return StreamingResponse(iter_upstream(), media_type="application/json")


@scheduler.post("/v1/completions")
async def create_completions(request: FastAPIRequest):
    """
    对应：
      curl http://HOST:PORT/v1/completions -H "Content-Type: application/json" -d '{...}'
    payload 结构类似 OpenAI completions 接口：
      {
        "model": "...",
        "prompt": "...." 或 ["..."],
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

    # 取客户端 IP 和路径（用于构建内部 Request）
    client_ip = request.client.host if request.client else "unknown"
    url_path = request.url.path

    # 把用户请求头转成 dict[str, str]
    raw_headers = {k.lower(): v for k, v in request.headers.items()}

    # 构建内部 Request（包含 Prompt/Service/Task 等）
    pool = get_pool()
    proxy_infos = await pool.list(include_dead=False)
    proxies = [
        {
            "proxy_id": p.proxy_id,
            "host": p.host,
            "port": p.port,
            "inflight": p.load.inflight,
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

    # 根据调度结果选择下游 URL，更新对应proxy_id的inflight
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

    # 转化payload
    data_for_downstream = req_obj.to_payload()

    # 处理需要透传给下游的 headers（可选择性过滤）
    extra_headers = {}

    # 把用户的 Authorization 原样传给下游（如果希望由 Proxy 做鉴权）
    if "authorization" in raw_headers:
        extra_headers["authorization"] = raw_headers["authorization"]

    # 带一个 Scheduler分配的Request ID，便于链路追踪
    extra_headers["scheduler-request-id"] = str(req_obj.Request_ID)

    # 定义一个 async 生成器，从下游流式读取，再回给上游
    async def iter_upstream():
        try:
            # forward_request 本身是一个 async generator
            async for content in forward_request(
                    url=downstream_url,
                    data=data_for_downstream,
                    use_chunked=False,  # chat/completions 一般是流式
                    extra_headers=extra_headers,  # 透传必要头
            ):
                # 这里 chunk 已经是 bytes，直接 yield 出去即可
                yield content
        finally:
            await pool.inflight_delta(proxy_id, -1)

    # 用 StreamingResponse 包一层，实现用户侧的流式响应
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

    if table is None:

        return {
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
            "proxies": proxy_states,
        }

    units = getattr(table, "_units", {})
    kids = list(units.keys())
    kids_sorted = sorted(kids)[:10]

    # 尽量从 table 拿到 dim
    dim = getattr(table, "dim", None)

    # FAISS
    faiss_total = None
    idx = getattr(table, "_faiss_index", None)
    if idx is not None:
        try:
            faiss_total = int(getattr(idx, "ntotal"))
        except Exception:
            faiss_total = None

    # KnowledgeUnit 字段探测（帮助你看 snapshot 后到底存了什么）
    unit_fields: List[str] = []
    if kids_sorted:
        u0 = units[kids_sorted[0]]
        # dataclass 一般有 __dict__
        if hasattr(u0, "__dict__"):
            unit_fields = sorted(list(u0.__dict__.keys()))
        else:
            # fallback：dir 过滤
            unit_fields = [x for x in dir(u0) if not x.startswith("_")]

    return {
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

        # 可选：你若在 KnowledgeUnit/metadata 里存了 kv_ready 等，也可以在这里输出
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

    # 只返回简要信息，避免输出过大
    sample = [{
        "proxy_id": p.proxy_id,
        "host": p.host,
        "port": p.port,
        "is_alive": True,  # list(include_dead=False) 已保证 alive
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

# 预留：/knowledge/update 等路由以后再加
# @api.post("/knowledge/update")
# async def knowledge_update(request: FastAPIRequest):
#     payload = await request.json()
#     client_ip = request.client.host if request.client else "unknown"
#     ...
#     return JSONResponse(content={"status": "ok"})
