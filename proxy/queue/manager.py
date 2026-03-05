# proxy/queue/manager.py
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional, AsyncGenerator

from core import forward_request

from .task import ProxyTask
from .instance_queues import PerInstanceQueueMap
from .knowledge import fetch_knowledge_from_kdn, format_retrieved_context, inject_rag_into_instance_body

logger = logging.getLogger("proxy.queue")


class QueueManager:
    """
    Step2：per-instance prepare/ready 队列 + worker。

    注意：
    - Step2 只实现 text 注入路径；
    - KVCache 注入路径（Injection_type="kvcache"）先占位，Step3 再做。
    """

    def __init__(self) -> None:
        self._qmap = PerInstanceQueueMap()
        self._workers_started = False
        self._worker_tasks: Dict[str, asyncio.Task] = {}

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
        if f"prepare:{instance_id}" not in self._worker_tasks:
            self._worker_tasks[f"prepare:{instance_id}"] = asyncio.create_task(
                self._prepare_worker_loop(instance_id)
            )
        if f"ready:{instance_id}" not in self._worker_tasks:
            self._worker_tasks[f"ready:{instance_id}"] = asyncio.create_task(
                self._ready_worker_loop(instance_id)
            )

    async def enqueue_prepare(self, task: ProxyTask) -> None:
        """
        handler 调用：把任务放到 prepare 队列，然后立刻返回（handler 不再做注入）。
        """
        # 懒启动该 instance 的 workers
        self._start_workers_for_instance(task.instance_id)

        q = self._qmap.get(task.instance_id)
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

    async def _prepare_worker_loop(self, instance_id: str) -> None:
        """
        prepare worker：负责知识准备（Step2 只实现 text 注入）。
        """
        q = self._qmap.get(instance_id)
        while True:
            task = await q.prepare_q.get()
            try:
                svc = getattr(task.req_obj, "Service", None)
                enable_rag = bool(getattr(svc, "Enable_know_injection", False))
                knowledge_ids = getattr(svc, "Knowledge_List", []) or []
                injection_type = getattr(svc, "Injection_type", "text")

                # Step2：仅实现 text；kvcache 先占位，直接走 text 或先不注入（你后续决定）
                if enable_rag and knowledge_ids and injection_type == "text":

                    kdn_addr = task.kdn_addr
                    if not kdn_addr:
                        logger.warning("[Prepare] rid=%s no kdn_addr", task.request_id)
                        items, miss = [], []
                    else:
                        kdn_url = f"http://{kdn_addr}"
                        items, miss = await fetch_knowledge_from_kdn(
                            kdn_url,
                            knowledge_ids
                        )

                    ctx = format_retrieved_context(items)
                    if miss:
                        logger.info("[Prepare] rid=%s miss=%s", task.request_id, miss)

                    endpoint_type = getattr(svc, "Endpoint_type", "chat/completions")
                    task.instance_body = inject_rag_into_instance_body(
                        instance_body=task.instance_body,
                        endpoint_type=endpoint_type,
                        retrieved_context=ctx,
                    )
                    logger.info("[Prepare] done rid=%s ctx_len=%s", task.request_id, len(ctx))
                else:
                    logger.info(
                        "[Prepare] skip knowledge injection rid=%s enable_rag=%s injection=None kids=%s",
                        task.request_id, enable_rag, len(knowledge_ids)
                    )

                # 放入 ready 队列
                await q.ready_q.put(task)
                logger.info(
                    "[Queue] enqueue_ready rid=%s instance=%s ready_len=%s",
                    task.request_id, instance_id, q.ready_q.qsize()
                )
            except Exception as e:
                # prepare 失败：不阻断推理，退化为不注入，仍推到 ready
                task.error = f"prepare_failed: {e}"
                logger.exception("[Prepare] failed rid=%s fallback no-rag", task.request_id)
                await q.ready_q.put(task)

    async def _ready_worker_loop(self, instance_id: str) -> None:
        """
        ready worker：负责真正 forward 到 instance，并把结果写入 task.response_queue。
        """
        q = self._qmap.get(instance_id)
        while True:
            task = await q.ready_q.get()
            try:
                target_url = f"http://{task.instance_host}:{task.instance_port}{task.url_path}"
                logger.info("[Ready] forward rid=%s -> %s", task.request_id, target_url)

                # use_chunked: chat->True, completions->False（由 task.url_path 决定）
                use_chunked = True if task.url_path.endswith("/chat/completions") else False

                async for chunk in forward_request(
                    url=target_url,
                    data=task.instance_body,
                    use_chunked=use_chunked,
                ):
                    if chunk:
                        await task.response_queue.put(chunk)

                # 结束符
                await task.response_queue.put(None)
                logger.info("[Ready] done rid=%s stream_end=1", task.request_id)

            except Exception as e:
                task.error = f"ready_failed: {e}"
                logger.exception("[Ready] failed rid=%s", task.request_id)
                # 出错也要通知 handler 结束，否则上游会一直挂着
                await task.response_queue.put(None)