# kdn_server/kdn_api.py
from __future__ import annotations

import asyncio
import os, time
import shutil,logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from model import EmbeddingEngine
from pydantic import BaseModel

from .text_db import TextDatabase
from .kv_builder import KVBuildConfig, KVCacheBuilder
from .sclient.scheduler_client import SchedulerClient
from core import config
from kdn_server.kv_injector import KVCacheInjector



kdn = FastAPI(title="KDN Server v2")

_TEXT_DB: Optional[TextDatabase] = None
_SCHED_CLI: SchedulerClient | None = None
_HB_TASK: asyncio.Task | None = None
_HB_STOP: asyncio.Event | None = None
_KDN_ID: str = ""


class InjectReadyKVReq(BaseModel):
    request_id: int
    model: str
    knowledge_ids: List[str] = []
    redis_host: str
    redis_port: int
    redis_db: int = 0
    redis_password: Optional[str] = None



def _get_db_dir() -> Path:
    # 允许 demo 里配置，不在模块里写死
    db_dir = os.getenv("KDN_TEXT_DB_DIR", "").strip()
    if db_dir:
        return Path(db_dir).resolve()
    # 默认放在 kdn_server/text_database
    return (Path(__file__).resolve().parent / "text_database").resolve()

def _get_kv_root_dir() -> Path:
    kv_dir = os.getenv("KDN_KV_DB_DIR", "").strip()
    if kv_dir:
        return Path(kv_dir).resolve()
    # 默认与 text_database 同级：kdn_server/KV_database
    return (Path(__file__).resolve().parent / "KV_database").resolve()


@kdn.on_event("startup")
async def _startup():
    global _TEXT_DB, _SCHED_CLI, _HB_TASK, _HB_STOP, _KDN_ID

    # 1) 原有 TextDB 初始化逻辑（保持不变）
    db_dir = _get_db_dir()
    embedding_model = os.getenv("KDN_EMBEDDING_MODEL")
    print(f"[KDN] KDN_EMBEDDING_MODEL={embedding_model!r}")

    embedder = None

    if embedding_model:
        try:
            print(f"[KDN] loading embedding model from: {embedding_model}")
            embedder = EmbeddingEngine(embedding_model)
            print("[KDN] embedding model loaded successfully")
        except Exception as e:
            print(f"[KDN] failed to load embedding model: {e}")
            embedder = None
    else:
        print("[KDN] KDN_EMBEDDING_MODEL not set")

    _TEXT_DB = TextDatabase(str(db_dir), embedder=embedder)
    print(f"[KDN] TextDatabase ready: {db_dir}")

    # 2) scheduler registration (optional but enabled by env)
    sched_url = os.getenv("SCHEDULER_CP_URL", "").strip()
    if not sched_url:
        print("[KDN] SCHEDULER_CP_URL not set, skip scheduler registration")
        return

    _KDN_ID = os.getenv("KDN_ID", "").strip() or f"kdn_{int(time.time())}"

    adv_host = os.getenv("KDN_ADVERTISE_HOST", "").strip()
    adv_port = os.getenv("KDN_ADVERTISE_PORT", "").strip()

    if not adv_host or not adv_port:
        print("[KDN] KDN_ADVERTISE_HOST/PORT not set, skip scheduler registration")
        return

    _SCHED_CLI = SchedulerClient(scheduler_cp_url=sched_url)

    try:
        r = await _SCHED_CLI.register(
            kdn_id=_KDN_ID,
            host=adv_host,
            port=int(adv_port),
            endpoints=["knowledge/snapshot", "knowledge/search/text"],
            tags=[],
            weight=1.0,
            meta={"db_dir": str(db_dir)},
        )
        hb_interval = int(r.get("heartbeat_interval_s", 10))
        print(f"[KDN] registered to scheduler: kdn_id={_KDN_ID} addr={adv_host}:{adv_port} hb={hb_interval}s")
    except Exception as e:
        print(f"[KDN] register to scheduler failed: {e}")
        return

    # 3) heartbeat loop
    _HB_STOP = asyncio.Event()

    async def _hb_loop():
        assert _SCHED_CLI is not None
        while not _HB_STOP.is_set():
            try:
                await _SCHED_CLI.heartbeat(_KDN_ID)
            except Exception as e:
                print(f"[KDN] heartbeat failed: {e}")
            try:
                await asyncio.wait_for(_HB_STOP.wait(), timeout=max(2, hb_interval))
            except asyncio.TimeoutError:
                pass

    _HB_TASK = asyncio.create_task(_hb_loop())


@kdn.post("/knowledge/snapshot")
async def knowledge_snapshot(payload: Dict[str, Any]):
    """
    Scheduler 启动时拉取 KDN 知识库快照：
      {
        "need_fields": ["kid","length","embedding","embed_dim","kv_ready","kv_rel_dir","kv_dumped_keys","kv_updated_at","rel_path"],
        "limit": 1000000,
        "offset": 0
      }

    返回：
      { "items": [...], "count": N, "limit": L, "offset": O }
    """
    if _TEXT_DB is None:
        return JSONResponse(status_code=500, content={"error": "TextDatabase not initialized"})

    need_fields = payload.get("need_fields") or []
    if not isinstance(need_fields, list):
        return JSONResponse(status_code=400, content={"error": "need_fields must be a list"})

    # 默认给足建库需要字段
    if not need_fields:
        need_fields = [
            "kid", "length", "embedding", "embed_dim",
            "kv_ready", "kv_rel_dir", "kv_dumped_keys", "kv_updated_at", "rel_path",
        ]

    limit = int(payload.get("limit", 1000000))
    offset = int(payload.get("offset", 0))

    include_embedding = ("embedding" in need_fields)
    rows = _TEXT_DB.snapshot(limit=limit, offset=offset, include_embedding=include_embedding)

    # --- 字段投影：只返回 scheduler 要的字段，避免无用大字段 ---
    allow = set(need_fields)
    items = []
    for r in rows:
        item = {}
        # KDN 对外字段名统一：kid
        if "kid" in allow:
            item["kid"] = r.get("kid")
        if "rel_path" in allow:
            item["rel_path"] = r.get("rel_path")
        if "length" in allow:
            item["length"] = r.get("length")
        if "embed_dim" in allow:
            item["embed_dim"] = r.get("embed_dim")
        if "embedding" in allow:
            item["embedding"] = r.get("embedding")
        if "kv_ready" in allow:
            item["kv_ready"] = r.get("kv_ready")
        if "kv_rel_dir" in allow:
            item["kv_rel_dir"] = r.get("kv_rel_dir")
        if "kv_dumped_keys" in allow:
            item["kv_dumped_keys"] = r.get("kv_dumped_keys")
        if "kv_updated_at" in allow:
            item["kv_updated_at"] = r.get("kv_updated_at")

        items.append(item)

    return JSONResponse(content={
        "items": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    })


@kdn.post("/knowledge/register_text")
async def register_text(payload: Dict[str, Any]):
    """
    外部系统 -> KDN 注册知识块：
      { "content": "...", "meta": { ... optional ... } }

    返回：
      { "kid": "<sha256>", "status": "created|exists", "length": 123 }
    """
    if _TEXT_DB is None:
        return JSONResponse(status_code=500, content={"error": "TextDatabase not initialized"})

    content = payload.get("content")
    meta = payload.get("meta")

    if not isinstance(content, str):
        return JSONResponse(status_code=400, content={"error": "content must be a string"})
    if meta is not None and not isinstance(meta, dict):
        return JSONResponse(status_code=400, content={"error": "meta must be an object if provided"})

    try:
        kid, status, length = _TEXT_DB.register_text(content=content, meta=meta)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    return JSONResponse(content={"kid": kid, "status": status, "length": length})

@kdn.post("/knowledge/build_kv")
async def build_kv(payload: Dict[str, Any]):
    """
    触发为某个 kid 生成 KVCache dump 并落盘到 KV_database/<kid>/。

    请求（最小）：
      { "kid": "<kid>", "api_url": "...", "model": "..." }

    可选参数：
      max_tokens, temperature,
      redis_host, redis_port, redis_db, redis_password,
      match, scan_count, flushdb
    """
    if _TEXT_DB is None:
        return JSONResponse(status_code=500, content={"error": "TextDatabase not initialized"})

    kid = payload.get("kid")
    if not isinstance(kid, str) or not kid.strip():
        return JSONResponse(status_code=400, content={"error": "kid must be a string"})

    api_url = payload.get("api_url")
    model = payload.get("model")
    if not isinstance(api_url, str) or not api_url.strip():
        return JSONResponse(status_code=400, content={"error": "api_url must be a string"})
    if not isinstance(model, str) or not model.strip():
        return JSONResponse(status_code=400, content={"error": "model must be a string"})

    # 1) 从 TextDatabase 取文本（必须完全一致，确保后续复用命中）
    items, miss = _TEXT_DB.get_many([kid.strip().lower()])
    if miss or not items:
        return JSONResponse(status_code=404, content={"error": f"kid not found: {kid}"})

    kb = items[0]  # 你当前 get_many 保证顺序
    text = kb.content

    # 2) 组装 KV build 配置
    kv_root = str(_get_kv_root_dir())
    cfg = KVBuildConfig(
        kv_root=kv_root,
        api_url=api_url,
        model=model,
        max_tokens=int(payload.get("max_tokens", 1)),
        temperature=float(payload.get("temperature", 0.0)),
        redis_host=str(payload.get("redis_host", "127.0.0.1")),
        redis_port=int(payload.get("redis_port", 6379)),
        redis_db=int(payload.get("redis_db", 0)),
        redis_password=payload.get("redis_password", None),
        match=str(payload.get("match", "vllm@*")),
        scan_count=int(payload.get("scan_count", 1000)),
        flushdb=bool(payload.get("flushdb", False)),
    )

    # 3) 触发 build（覆盖刷新 KV_database/<kid>/）
    try:
        # 如果你已把 kv_builder 做成 build 后自动回写 mark_kv_ready，可直接传 _TEXT_DB
        builder = KVCacheBuilder(cfg, text_db=_TEXT_DB)  # 若你还没改 kv_builder，这里会报参数不匹配
        out = builder.build_from_text(text)
    except TypeError:
        # 兼容你尚未修改 kv_builder 构造函数的情况
        builder = KVCacheBuilder(cfg)
        out = builder.build_from_text(text)

        # 如果你已经在 text_db.py 里实现了 mark_kv_ready，就在这里回写
        if hasattr(_TEXT_DB, "mark_kv_ready"):
            try:
                _TEXT_DB.mark_kv_ready(
                    kid=out["kid"],
                    kv_rel_dir=out["kid"],          # 推荐：索引里只存 kid 作为相对目录名
                    dumped_keys=int(out.get("dumped_keys", 0)),
                )
            except Exception:
                # 回写失败不影响 build 成功，但你应该在日志里看到（此处先不打断）
                pass

    return JSONResponse(content={
        "kid": out.get("kid"),
        "kv_dir": out.get("kv_dir"),
        "dumped_keys": out.get("dumped_keys"),
        "kv_root": kv_root,
    })


@kdn.post("/knowledge/search/text")
async def knowledge_text(payload: Dict[str, Any]):
    """
    Proxy -> KDN 查询：
      { "knowledge_ids": ["<kid1>", "<kid2>", ...], "need_fields": ["content","length"] }

    KDN -> Proxy 响应：
      { "items": [{"id":"<kid>","content":"...","length":180}, ...], "miss":["<kid3>"] }
    """
    if _TEXT_DB is None:
        return JSONResponse(status_code=500, content={"error": "TextDatabase not initialized"})

    ids = payload.get("knowledge_ids") or []
    if not isinstance(ids, list):
        return JSONResponse(status_code=400, content={"error": "knowledge_ids must be a list"})

    # kid：统一转成小写字符串；不强制长度为 64（方便你未来做短ID展示），但索引里用全长
    kids: List[str] = []
    for x in ids:
        if isinstance(x, str):
            kids.append(x.strip().lower())
        else:
            return JSONResponse(status_code=400, content={"error": "knowledge_ids elements must be strings (kid)"})

    items, miss = _TEXT_DB.get_many(kids)
    need_fields = payload.get("need_fields") or []
    if need_fields and not isinstance(need_fields, list):
        return JSONResponse(status_code=400, content={"error": "need_fields must be a list"})

    allow = set(need_fields) if need_fields else None

    def _pick(it):
        d = {"id": it.id}  # 保持旧字段 id，避免 proxy 侧再改一堆
        # 无 need_fields → 保持原行为（兼容）
        if allow is None:
            d.update({
                "content": it.content,
                "length": it.length,
                "rel_path": it.rel_path,
                "embedding": it.embedding,
                "embed_dim": it.embed_dim,
                "kv_ready": it.kv_ready,
                "kv_rel_dir": it.kv_rel_dir,
                "kv_dumped_keys": it.kv_dumped_keys,
                "kv_updated_at": it.kv_updated_at,
            })
            return d

        # 有 need_fields → 只返回指定字段
        if "content" in allow: d["content"] = it.content
        if "length" in allow: d["length"] = it.length
        if "rel_path" in allow: d["rel_path"] = it.rel_path
        if "embed_dim" in allow: d["embed_dim"] = it.embed_dim
        if "embedding" in allow: d["embedding"] = it.embedding
        if "embedding_head" in allow:
            d["embedding_head"] = (it.embedding[:10] if it.embedding else None)
        if "kv_ready" in allow: d["kv_ready"] = it.kv_ready
        if "kv_rel_dir" in allow: d["kv_rel_dir"] = it.kv_rel_dir
        if "kv_dumped_keys" in allow: d["kv_dumped_keys"] = it.kv_dumped_keys
        if "kv_updated_at" in allow: d["kv_updated_at"] = it.kv_updated_at
        return d

    resp_items = [_pick(it) for it in items]
    return JSONResponse(content={"items": resp_items, "miss": miss})


@kdn.post("/knowledge/delete")
async def delete_knowledge(payload: Dict[str, Any]):
    """
    删除知识块（默认仅删 text_database 的索引+txt；可选同时删 KV_database/<kid>/）。
    Body:
      {
        "knowledge_ids": ["kid1", "kid2"],
        "delete_kv": true
      }
    """
    if _TEXT_DB is None:
        return JSONResponse(status_code=500, content={"error": "TextDatabase not initialized"})

    kids = payload.get("knowledge_ids")
    if isinstance(kids, str):
        kids = [kids]
    if not isinstance(kids, list) or not kids:
        return JSONResponse(status_code=400, content={"error": "knowledge_ids must be a non-empty list"})

    delete_kv = bool(payload.get("delete_kv", False))

    # 1) 删除 text_database（索引 + txt）
    res = _TEXT_DB.delete_many([str(k).strip().lower() for k in kids])

    # 2) 可选：删除 KV_database/<kid>/（仅对实际 deleted 的 kid 执行）
    kv_deleted = []
    kv_errors = []
    if delete_kv:
        kv_root = _get_kv_root_dir()
        for kid in res.get("deleted", []):
            d = (kv_root / kid).resolve()
            try:
                if d.exists():
                    shutil.rmtree(d)
                kv_deleted.append(kid)
            except Exception as e:
                kv_errors.append({"kid": kid, "error": str(e)})

    return JSONResponse(content={
        "deleted": res.get("deleted", []),
        "not_found": res.get("not_found", []),
        "errors": res.get("errors", []),
        "kv_deleted": kv_deleted,
        "kv_errors": kv_errors,
    })

@kdn.post("/knowledge/purge_all")
async def purge_all(payload: Dict[str, Any]):
    """
    清空整个 KDN 数据库（危险操作）：
    - 删除 text_database 下 blocks/ 全部 txt
    - 删除 index.sqlite3（或重建为空）
    - 可选：删除 KV_database 下全部 kid 目录

    Body:
      { "delete_kv": true }
    """
    if _TEXT_DB is None:
        return JSONResponse(status_code=500, content={"error": "TextDatabase not initialized"})

    delete_kv = bool((payload or {}).get("delete_kv", True))

    db_dir = _get_db_dir()
    kv_root = _get_kv_root_dir()

    # 1) 删除 SQLite
    db_path = db_dir / "index.sqlite3"

    # 2) 删除 blocks 目录下所有 txt
    blocks_dir = db_dir / "blocks"

    # 3) 可选：删除 KV_database 下所有目录
    kv_deleted = False

    errors = []

    # 先尽量删 blocks（不依赖 sqlite）
    try:
        if blocks_dir.exists():
            shutil.rmtree(blocks_dir)
            blocks_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        errors.append({"part": "text_blocks", "error": str(e)})

    # 删除 sqlite 文件（以及 sqlite 的 wal/shm）
    try:
        for p in [db_path, db_path.with_suffix(".sqlite3-wal"), db_path.with_suffix(".sqlite3-shm")]:
            if p.exists():
                p.unlink()
    except Exception as e:
        errors.append({"part": "sqlite", "error": str(e)})

    # 重建一个空库，避免服务继续运行时报 “no such table”
    try:
        _TEXT_DB._init_db()
        _TEXT_DB._ensure_kv_columns()
        _TEXT_DB._ensure_embedding_columns()
    except Exception as e:
        errors.append({"part": "reinit_db", "error": str(e)})

    if delete_kv:
        try:
            if kv_root.exists():
                shutil.rmtree(kv_root)
            kv_root.mkdir(parents=True, exist_ok=True)
            kv_deleted = True
        except Exception as e:
            errors.append({"part": "kv_root", "error": str(e)})

    return JSONResponse(content={
        "purged": True,
        "delete_kv": delete_kv,
        "db_dir": str(db_dir),
        "kv_root": str(kv_root),
        "kv_deleted": kv_deleted,
        "errors": errors,
    })


@kdn.on_event("shutdown")
async def _shutdown():
    global _SCHED_CLI, _HB_TASK, _HB_STOP, _KDN_ID

    if _HB_STOP is not None:
        _HB_STOP.set()
    if _HB_TASK is not None:
        _HB_TASK.cancel()

    if _SCHED_CLI is not None and _KDN_ID:
        try:
            await _SCHED_CLI.unregister(_KDN_ID)
            print(f"[KDN] unregistered from scheduler: kdn_id={_KDN_ID}")
        except Exception as e:
            print(f"[KDN] unregister failed: {e}")

        try:
            await _SCHED_CLI.close()
        except Exception:
            pass


@kdn.post("/knowledge/inject_ready_kv")
async def inject_ready_kv(payload: Dict[str, Any]):
    """
    运行时 KV 注入接口：
    - 只按 kid 查询已有状态
    - 只对 kv_ready=1 的 kid 执行注入
    - 不做 build
    """
    if _TEXT_DB is None:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "injected_kids": [],
                "text_only_kids": [],
                "miss_kids": payload.get("knowledge_ids", []),
                "keys_injected": 0,
                "detail": "text_db is not ready",
            },
        )

    requested = [str(x).strip().lower() for x in (payload.get("knowledge_ids") or [])]
    if not requested:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "detail": "knowledge_ids is empty"},
        )

    redis_host = str(payload.get("redis_host", "127.0.0.1"))
    redis_port = int(payload.get("redis_port", 6379))
    redis_db = int(payload.get("redis_db", 0))
    redis_password = payload.get("redis_password", None)

    # 这里一定要拆包：get_many -> (items, miss)
    items, miss = _TEXT_DB.get_many(requested)

    # items 里的元素是 KBItem，不是 dict
    row_map = {str(it.id): it for it in items}

    injected_kids: List[str] = []
    text_only_kids: List[str] = []
    miss_kids: List[str] = list(miss or [])
    total_keys = 0

    kv_root = str(_get_kv_root_dir())

    injector = KVCacheInjector(
        redis_host=redis_host,
        redis_port=redis_port,
        redis_db=redis_db,
        redis_password=redis_password,
    )

    for kid in requested:
        if kid in miss_kids:
            continue

        row = row_map.get(kid)
        if row is None:
            # 理论上 get_many 已经把 miss 给出来了，这里兜底
            miss_kids.append(kid)
            continue

        # KBItem 字段访问用属性，不是 get()
        if not bool(row.kv_ready):
            text_only_kids.append(kid)
            continue

        # 运行时注入路径只认 kid，不信任 kv_rel_dir 的格式
        kv_dir = os.path.join(kv_root, kid)

        try:
            res = injector.inject_kv_dir(kv_dir)

            # 只有真正注入完成后才记 success
            injected_kids.append(kid)
            total_keys += int(res.injected)

            logging.info(
                "[KDN] inject_ready_kv ok: kid=%s kv_dir=%s injected=%s missing_files=%s",
                kid, kv_dir, res.injected, res.missing_files
            )

        except Exception as e:
            # 注入失败时先降级为 text_only，不阻断业务
            text_only_kids.append(kid)
            logging.exception(
                "[KDN] inject_ready_kv failed: kid=%s kv_dir=%s err=%s",
                kid, kv_dir, str(e)
            )

    return JSONResponse(
        content={
            "ok": True,
            "injected_kids": injected_kids,
            "text_only_kids": text_only_kids,
            "miss_kids": miss_kids,
            "keys_injected": total_keys,
            "detail": "",
        }
    )