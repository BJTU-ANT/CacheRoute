# proxy/queue/manager.py
from __future__ import annotations

import os
import time
import httpx
import asyncio
import logging
from typing import Dict, Optional, AsyncGenerator, Any

from core import forward_request,config

from .task import ProxyTask
from .instance_queues import PerInstanceQueueMap
from .knowledge import (
    fetch_knowledge_from_kdn,
    format_retrieved_context,
    inject_rag_into_instance_body,
    build_ordered_context,
    classify_kdn_items,
)

logger = logging.getLogger("proxy.queue")

def _now_ms() -> int:
    return int(time.time() * 1000)


class QueueManager:
    """
    Step2：per-instance prepare/ready 队列 + worker。

    注意：
    - Step2 只实现 text 注入路径；
    - KVCache 注入路径（Injection_type="kvcache"）先占位，Step3 再做。
    """

    def __init__(self) -> None:
        self._prepare_concurrency_per_instance = int(os.environ.get("PREPARE_CONCURRENCY", config.PREPARE_CONCURRENCY))
        self._ready_concurrency_per_instance = int(os.environ.get("READY_CONCURRENCY", getattr(config, "READY_CONCURRENCY", 8))
        )

        self._qmap = PerInstanceQueueMap(
            prepare_concurrency_per_instance=self._prepare_concurrency_per_instance,
            ready_concurrency_per_instance = self._ready_concurrency_per_instance,
        )

        self._workers_started = False
        self._worker_tasks: Dict[str, asyncio.Task] = {}
        self._http_timeout_s = 60.0

    def ensure_workers_started(self, instance_ids: Optional[list[str]] = None) -> None:
        """
        启动 worker（只启动一次）。
        - Step2 简化：你可以先在 proxy 启动后调用一次，不要求动态增减实例。
        - 后续我们可以做：实例注册时动态启动对应 worker。
        """
        if self._workers_started:
            return
        self._workers_started = True

        # Step2：不强依赖 instance_ids，worker 在第一次 enqueue 时也能懒启动。
        if instance_ids:
            for iid in instance_ids:
                self._start_workers_for_instance(iid)

    def _start_workers_for_instance(self, instance_id: str) -> None:
        if f"prepare_dispatch:{instance_id}" not in self._worker_tasks:
            self._worker_tasks[f"prepare_dispatch:{instance_id}"] = asyncio.create_task(
                self._prepare_dispatch_loop(instance_id)
            )

        for worker_idx in range(self._ready_concurrency_per_instance):
            ready_key = f"ready:{instance_id}:{worker_idx}"
            if ready_key not in self._worker_tasks:
                self._worker_tasks[ready_key] = asyncio.create_task(
                    self._ready_worker_loop(instance_id, worker_idx)
                )

    async def enqueue_prepare(self, task: ProxyTask) -> None:
        """
        handler 调用：把任务放到 prepare 队列，然后立刻返回（handler 不再做注入）。
        """
        # 懒启动该 instance 的 workers
        self._start_workers_for_instance(task.instance_id)

        q = self._qmap.get(task.instance_id)
        task.trace["proxy_enqueue_ms"] = _now_ms()

        await q.prepare_q.put(task)
        logger.info(
            "[Queue] enqueue_prepare rid=%s instance=%s prepare_len=%s injection=%s",
            task.request_id, task.instance_id, q.prepare_q.qsize(),
            getattr(getattr(task.req_obj, "Service", None), "Injection_type", None),
        )

    async def iter_response(self, task: ProxyTask) -> AsyncGenerator[bytes, None]:
        """
        handler 调用：从 task.response_queue 迭代读取 bytes，直到收到 None 结束符。
        """
        while True:
            chunk = await task.response_queue.get()
            if chunk is None:
                break
            yield chunk

    async def _run_prepare_task(self, instance_id: str, task: ProxyTask) -> None:
        """
        单个任务的 prepare 流程。
        同一 instance 下可并发执行，但受 prepare_sem 限制。
        """
        q = self._qmap.get(instance_id)

        async with q.prepare_sem:
            q.active_prepare += 1
            try:
                task.trace["prepare_start_ms"] = _now_ms()

                svc = getattr(task.req_obj, "Service", None)
                enable_rag = bool(getattr(svc, "Enable_know_injection", False))
                knowledge_ids = getattr(svc, "Knowledge_List", []) or []
                injection_type = getattr(svc, "Injection_type", "text")

                if enable_rag and knowledge_ids and injection_type in ("text", "kvcache"):
                    kdn_addr = (task.kdn_addr or "").strip()
                    if not kdn_addr:
                        logger.warning("[Prepare] rid=%s no kdn_addr", task.request_id)
                        items, miss = [], [str(x) for x in knowledge_ids]
                    else:
                        task.trace["kdn_fetch_start_ms"] = _now_ms()
                        kdn_url = f"http://{kdn_addr}"
                        items, miss = await fetch_knowledge_from_kdn(
                            kdn_url,
                            [str(x) for x in knowledge_ids],
                        )
                        task.trace["kdn_fetch_end_ms"] = _now_ms()

                    classified = classify_kdn_items(
                        requested_ids=[str(x) for x in knowledge_ids],
                        items=items,
                        miss=miss,
                    )

                    kv_ready_items = classified["kv_ready_items"]
                    text_only_items = classified["text_only_items"]
                    miss_ids = classified["miss_ids"]

                    ctx = build_ordered_context(
                        kv_ready_items=kv_ready_items,
                        text_only_items=text_only_items,
                    )

                    endpoint_type = getattr(svc, "Endpoint_type", "chat/completions")
                    task.instance_body = inject_rag_into_instance_body(
                        instance_body=task.instance_body,
                        endpoint_type=endpoint_type,
                        retrieved_context=ctx,
                        injection_type=injection_type,
                    )
                    task.trace["prompt_injected_ms"] = _now_ms()

                    task.kv_ready_kids = [
                        str(it.get("knowledge_id") or it.get("kid") or it.get("id") or "")
                        for it in kv_ready_items
                    ]
                    task.text_only_kids = [
                        str(it.get("knowledge_id") or it.get("kid") or it.get("id") or "")
                        for it in text_only_items
                    ]
                    task.miss_kids = [str(x) for x in miss_ids]

                    logger.info(
                        "[Prepare] rid=%s classify done: kv_ready=%s text_only=%s miss=%s ctx_len=%s injection=%s active_prepare=%s",
                        task.request_id,
                        task.kv_ready_kids,
                        task.text_only_kids,
                        task.miss_kids,
                        len(ctx),
                        injection_type,
                        q.active_prepare,
                    )

                    if injection_type == "kvcache":
                        if task.kv_ready_kids:
                            try:
                                task.trace["kv_ack_start_ms"] = _now_ms()
                                kv_ack = await self._inject_ready_kv_via_instance(task)
                                task.trace["kv_ack_end_ms"] = _now_ms()

                                task.kv_ack = kv_ack
                                logger.info(
                                    "[Prepare] rid=%s kv_ack ok=%s injected=%s text_only=%s miss=%s keys=%s",
                                    task.request_id,
                                    kv_ack.get("ok"),
                                    kv_ack.get("injected_kids", []),
                                    kv_ack.get("text_only_kids", []),
                                    kv_ack.get("miss_kids", []),
                                    kv_ack.get("keys_injected", 0),
                                )
                            except Exception as e:
                                task.trace["kv_ack_end_ms"] = _now_ms()
                                task.kv_ack = {
                                    "ok": False,
                                    "injected_kids": [],
                                    "text_only_kids": list(task.kv_ready_kids) + list(task.text_only_kids),
                                    "miss_kids": list(task.miss_kids),
                                    "keys_injected": 0,
                                    "detail": str(e),
                                }
                                logger.exception("[Prepare] rid=%s kv inject ack failed, fallback text-only",
                                                 task.request_id)
                        else:
                            task.kv_ack = {
                                "ok": True,
                                "injected_kids": [],
                                "text_only_kids": list(task.text_only_kids),
                                "miss_kids": list(task.miss_kids),
                                "keys_injected": 0,
                                "detail": "no kv_ready_kids",
                            }
                            logger.info("[Prepare] rid=%s no kv_ready_kids, fallback text-only", task.request_id)
                else:
                    logger.info(
                        "[Prepare] skip knowledge injection request_id(rid)=%s enable_rag=%s injection=None kids=%s",
                        task.request_id, enable_rag, len(knowledge_ids)
                    )

                task.trace["ready_enqueue_ms"] = _now_ms()
                await q.ready_q.put(task)
                logger.info(
                    "[Queue] enqueue_ready rid=%s instance=%s ready_len=%s",
                    task.request_id, instance_id, q.ready_q.qsize()
                )

            except Exception as e:
                task.error = f"prepare_failed: {e}"
                logger.exception("[Prepare] failed rid=%s fallback no-rag", task.request_id)
                task.trace["ready_enqueue_ms"] = _now_ms()
                await q.ready_q.put(task)
            finally:
                q.active_prepare = max(0, q.active_prepare - 1)

    async def _ready_worker_loop(self, instance_id: str, worker_idx: int) -> None:
        """
        ready worker：负责真正 forward 到 instance，并把结果写入 task.response_queue。
        多个 worker 共享同一个 ready_q，从而形成 per-instance ready 并发窗口。
        """
        q = self._qmap.get(instance_id)
        while True:
            task = await q.ready_q.get()
            q.active_ready += 1
            try:
                task.trace["ready_dequeue_ms"] = _now_ms()
                task.trace["ready_worker_idx"] = worker_idx

                target_url = f"http://{task.instance_host}:{task.instance_port}{task.url_path}"
                logger.info(
                    "[Ready] worker=%s forward rid=%s -> %s active_ready=%s ready_q=%s",
                    worker_idx,
                    task.request_id,
                    target_url,
                    q.active_ready,
                    q.ready_q.qsize(),
                )

                # use_chunked: chat->True, completions->False（由 task.url_path 决定）
                use_chunked = True if task.url_path.endswith("/chat/completions") else False
                task.trace["forward_start_ms"] = _now_ms()

                seen_first_chunk = False
                async for chunk in forward_request(
                    url=target_url,
                    data=task.instance_body,
                    use_chunked=use_chunked,
                ):
                    if chunk:
                        if not seen_first_chunk:
                            seen_first_chunk = True
                            task.trace["first_token_ms"] = _now_ms()
                        await task.response_queue.put(chunk)

                task.trace["forward_end_ms"] = _now_ms()

                # 结束符
                await task.response_queue.put(None)
                logger.info(
                    "[Ready] worker=%s done rid=%s active_ready=%s",
                    worker_idx,
                    task.request_id,
                    q.active_ready,
                )

            except Exception as e:
                task.error = f"ready_failed: {e}"
                task.trace["forward_end_ms"] = _now_ms()
                logger.exception("[Ready] worker=%s failed rid=%s", worker_idx, task.request_id)
                # 出错也要通知 handler 结束，否则上游会一直挂着
                await task.response_queue.put(None)
            finally:
                q.active_ready = max(0, q.active_ready - 1)

    async def _prepare_dispatch_loop(self, instance_id: str) -> None:
        """
        从 prepare_q 取任务，但不串行执行。
        每个任务单独 create_task，由 _run_prepare_task 真正处理。
        并发上限由 q.prepare_sem 控制。
        """
        q = self._qmap.get(instance_id)
        while True:
            task = await q.prepare_q.get()
            asyncio.create_task(self._run_prepare_task(instance_id, task))

    async def _inject_ready_kv_via_instance(
            self,
            task: ProxyTask,
    ) -> Dict[str, Any]:
        """
        调 Instance 控制平面，请求对 kv_ready_kids 执行 KV 注入。
        """
        instance_cp_host = task.instance_host
        instance_cp_port = 9002  # 第一版先固定，后续可配到 config/env

        url = f"http://{instance_cp_host}:{instance_cp_port}/v1/kv/inject_ready"
        payload = {
            "request_id": int(task.request_id or 0),
            "kdn_addr": str(task.kdn_addr or ""),
            "model": getattr(getattr(task.req_obj, "Prompt", None), "model", ""),
            "knowledge_ids": list(task.kv_ready_kids or []),
        }

        async with httpx.AsyncClient(timeout=self._http_timeout_s) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()