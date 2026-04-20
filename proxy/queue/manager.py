# proxy/queue/manager.py
from __future__ import annotations

import os
import time
import httpx
import asyncio
import logging
from typing import Dict, Optional, AsyncGenerator, Any, List

from core import forward_request,config
from proxy.metrics.queue_predictor import queue_predictor
from proxy.resource import p_control_plane

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

    _PREDICT_HEADER_OVERHEAD_TOKENS = 36
    _KNOW_PREPARE_FIXED_OVERHEAD_MS = 3.0
    _READY_DEQUEUE_INTERVAL_S = 0.02

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
        # per-instance ready worker 抓取节流（顺序化 + 最小间隔）
        self._ready_fetch_locks: Dict[str, asyncio.Lock] = {}
        self._ready_last_fetch_ts_s: Dict[str, float] = {}
        # reservation shared state: slot layer + shared prefill layer
        self._instance_slot_free_ts_s: Dict[str, List[float]] = {}
        self._instance_prefill_free_ts_s: Dict[str, float] = {}
        self._instance_pending_tasks: Dict[str, List[ProxyTask]] = {}
        self._instance_next_reservation_seq: Dict[str, int] = {}
        self._reservation_locks: Dict[str, asyncio.Lock] = {}

    @staticmethod
    def _estimate_request_length(task: ProxyTask) -> int:
        """
        用于 TTFT 预测的长度口径：
          total_length = prompt_token_length + knowledge_length + header_overhead(36)
        """
        prompt = getattr(task.req_obj, "Prompt", None)
        service = getattr(task.req_obj, "Service", None)

        prompt_len = int(getattr(prompt, "token_length", 0) or 0)
        know_len = int(getattr(service, "Knowledge_length", 0) or 0)
        total_len = prompt_len + know_len + QueueManager._PREDICT_HEADER_OVERHEAD_TOKENS
        return max(1, total_len)

    async def _estimate_know_prepare_ms(self, task: ProxyTask) -> float:
        """
        估算知识准备时间：
          know_prepare_time = rtt(kdn->instance) + fixed_overhead(3ms)
        """
        kdn_addr = str(task.kdn_addr or "").strip()
        if not kdn_addr:
            return 0.0

        links = await p_control_plane.get_kdn_links_snapshot()
        item = (
            links.get(kdn_addr)
            or links.get(f"kdn://{kdn_addr}")
            or links.get(f"http://{kdn_addr}")
            or {}
        )
        rtt_ms = float(item.get("latency_ms", item.get("rtt_ms", 0.0)) or 0.0)
        return max(0.0, rtt_ms + self._KNOW_PREPARE_FIXED_OVERHEAD_MS)

    def _get_reservation_lock(self, instance_id: str) -> asyncio.Lock:
        lock = self._reservation_locks.get(instance_id)
        if lock is None:
            lock = asyncio.Lock()
            self._reservation_locks[instance_id] = lock
        return lock

    def _ensure_instance_reservation_state(self, instance_id: str, now_s: Optional[float] = None) -> None:
        ts_s = time.time() if now_s is None else now_s
        if instance_id not in self._instance_slot_free_ts_s:
            self._instance_slot_free_ts_s[instance_id] = [ts_s for _ in range(self._ready_concurrency_per_instance)]
        if instance_id not in self._instance_prefill_free_ts_s:
            self._instance_prefill_free_ts_s[instance_id] = ts_s
        if instance_id not in self._instance_pending_tasks:
            self._instance_pending_tasks[instance_id] = []
        if instance_id not in self._instance_next_reservation_seq:
            self._instance_next_reservation_seq[instance_id] = 1

    async def _reserve_ready_task(self, task: ProxyTask, now_s: Optional[float] = None) -> None:
        ts_s = time.time() if now_s is None else now_s
        ready_enqueue_ms = int(task.trace.get("ready_enqueue_ms", int(ts_s * 1000)))
        ready_enqueue_s = ready_enqueue_ms / 1000.0
        length = self._estimate_request_length(task)
        bs = 1
        compute_s = max(0.0, queue_predictor(length=length, bs=bs))
        know_prepare_ms = int(task.trace.get("predict_know_prepare_ms", 0) or 0)
        know_prepare_s = know_prepare_ms / 1000.0

        lock = self._get_reservation_lock(task.instance_id)
        async with lock:
            self._ensure_instance_reservation_state(task.instance_id, now_s=ts_s)
            slot_free = self._instance_slot_free_ts_s[task.instance_id]
            prefill_free_s = self._instance_prefill_free_ts_s[task.instance_id]
            slot_idx = min(range(len(slot_free)), key=lambda idx: slot_free[idx])
            slot_ready_s = max(ts_s, slot_free[slot_idx])
            prefill_start_s = max(slot_ready_s, prefill_free_s)
            # In current ordered-dispatch mode, forward start follows reservation prefill start.
            forward_start_s = prefill_start_s
            first_token_s = prefill_start_s + compute_s

            slot_free[slot_idx] = first_token_s
            self._instance_prefill_free_ts_s[task.instance_id] = first_token_s
            reservation_seq = self._instance_next_reservation_seq[task.instance_id]
            self._instance_next_reservation_seq[task.instance_id] = reservation_seq + 1

            task.pred_slot_idx = int(slot_idx)
            task.pred_slot_ready_ts_ms = int(slot_ready_s * 1000)
            task.pred_forward_start_ts_ms = int(forward_start_s * 1000)
            task.pred_prefill_start_ts_ms = int(prefill_start_s * 1000)
            task.pred_first_token_ts_ms = int(first_token_s * 1000)
            task.pred_service_ms = int(compute_s * 1000)
            task.reservation_seq = int(reservation_seq)
            task.recompute_generation = 0

            task.trace["predict_length_tokens"] = int(length)
            task.trace["predict_bs"] = int(bs)
            task.trace["pred_forward_start_ts_ms"] = task.pred_forward_start_ts_ms
            # queue_wait: ready_enqueue -> predicted forward_start
            task.trace["predict_queue_wait_ms"] = max(0, int((forward_start_s - ready_enqueue_s) * 1000))
            task.trace["predict_compute_ms"] = int(compute_s * 1000)
            # vllm_internal: from forward_start to first_token, includes vllm queue + prefill compute
            task.trace["predict_vllm_internal_ms"] = max(0, int((first_token_s - forward_start_s) * 1000))
            task.trace["predict_total_ms"] = int((know_prepare_s + (first_token_s - ready_enqueue_s)) * 1000)
            # compatibility field
            task.trace["predict_wait_ms"] = int(know_prepare_ms)

            pending = self._instance_pending_tasks[task.instance_id]
            pending.append(task)
            pending.sort(key=lambda t: t.reservation_seq)

            logger.debug(
                "[Reserve] rid=%s instance=%s seq=%s slot=%s slot_free=%s prefill_free=%.3f "
                "slot_ready=%.3f prefill_start=%.3f first_token=%.3f",
                task.request_id,
                task.instance_id,
                task.reservation_seq,
                slot_idx,
                [round(v, 3) for v in slot_free],
                prefill_free_s,
                slot_ready_s,
                prefill_start_s,
                first_token_s,
            )

    async def predict_new_task_wait_ms(self, instance_id: str) -> int:
        now_s = time.time()
        lock = self._get_reservation_lock(instance_id)
        async with lock:
            self._ensure_instance_reservation_state(instance_id, now_s=now_s)
            slot_free = self._instance_slot_free_ts_s[instance_id]
            slot_idx = min(range(len(slot_free)), key=lambda idx: slot_free[idx])
            slot_ready_s = max(now_s, slot_free[slot_idx])
            prefill_start_s = max(slot_ready_s, self._instance_prefill_free_ts_s[instance_id])
            return max(0, int((prefill_start_s - now_s) * 1000))

    async def _recompute_pending_from_now(self, instance_id: str, now_s: Optional[float] = None) -> None:
        ts_s = time.time() if now_s is None else now_s
        lock = self._get_reservation_lock(instance_id)
        async with lock:
            self._ensure_instance_reservation_state(instance_id, now_s=ts_s)
            pending = [t for t in self._instance_pending_tasks[instance_id] if not t.has_seen_first_token]
            pending.sort(key=lambda t: t.reservation_seq)

            # Rebuild reservation chain from real "now" baseline.
            slot_count = len(self._instance_slot_free_ts_s[instance_id])
            slot_free = [ts_s for _ in range(slot_count)]
            prefill_cursor_s = ts_s

            for task in pending:
                ready_enqueue_ms = int(task.trace.get("ready_enqueue_ms", int(ts_s * 1000)))
                ready_enqueue_s = ready_enqueue_ms / 1000.0
                slot_idx = min(range(len(slot_free)), key=lambda idx: slot_free[idx])
                slot_ready_s = max(ts_s, slot_free[slot_idx])
                prefill_start_s = max(slot_ready_s, prefill_cursor_s)
                forward_start_s = prefill_start_s
                service_s = max(0.0, task.pred_service_ms / 1000.0)
                first_token_s = prefill_start_s + service_s

                task.pred_slot_idx = int(slot_idx)
                task.pred_slot_ready_ts_ms = int(slot_ready_s * 1000)
                task.pred_forward_start_ts_ms = int(forward_start_s * 1000)
                task.pred_prefill_start_ts_ms = int(prefill_start_s * 1000)
                task.pred_first_token_ts_ms = int(first_token_s * 1000)
                task.recompute_generation += 1

                know_prepare_ms = int(task.trace.get("predict_know_prepare_ms", 0) or 0)
                task.trace["pred_forward_start_ts_ms"] = task.pred_forward_start_ts_ms
                task.trace["predict_queue_wait_ms"] = max(0, int((forward_start_s - ready_enqueue_s) * 1000))
                task.trace["predict_vllm_internal_ms"] = max(0, int((first_token_s - forward_start_s) * 1000))
                task.trace["predict_total_ms"] = int(know_prepare_ms + (first_token_s - ready_enqueue_s) * 1000)

                slot_free[slot_idx] = first_token_s
                prefill_cursor_s = first_token_s

            self._instance_pending_tasks[instance_id] = pending
            self._instance_slot_free_ts_s[instance_id] = slot_free
            self._instance_prefill_free_ts_s[instance_id] = prefill_cursor_s

    async def _mark_task_first_token_and_recompute(self, task: ProxyTask, instance_id: str) -> None:
        now_s = time.time()
        lock = self._get_reservation_lock(instance_id)
        async with lock:
            self._ensure_instance_reservation_state(instance_id, now_s=now_s)
            task.has_seen_first_token = True
            if 0 <= task.pred_slot_idx < len(self._instance_slot_free_ts_s[instance_id]):
                self._instance_slot_free_ts_s[instance_id][task.pred_slot_idx] = now_s
            # Important: recycle shared prefill timeline to the real current time.
            self._instance_prefill_free_ts_s[instance_id] = now_s
            self._instance_pending_tasks[instance_id] = [
                t for t in self._instance_pending_tasks[instance_id]
                if t is not task
            ]
        await self._recompute_pending_from_now(instance_id, now_s=now_s)

    async def _wait_dispatch_turn(self, task: ProxyTask, instance_id: str) -> None:
        """
        Gate ready forwarding by reservation_seq to keep real prefill order predictable.
        """
        while True:
            lock = self._get_reservation_lock(instance_id)
            async with lock:
                pending = [t for t in self._instance_pending_tasks.get(instance_id, []) if not t.has_seen_first_token]
                if not pending:
                    return
                pending.sort(key=lambda t: t.reservation_seq)
                if pending[0] is task:
                    task.has_started_forward = True
                    return
            await asyncio.sleep(0.005)

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
        self._ensure_instance_reservation_state(instance_id)
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

    def _get_ready_fetch_lock(self, instance_id: str) -> asyncio.Lock:
        lock = self._ready_fetch_locks.get(instance_id)
        if lock is None:
            lock = asyncio.Lock()
            self._ready_fetch_locks[instance_id] = lock
        return lock

    async def enqueue_prepare(self, task: ProxyTask) -> None:
        """
        handler 调用：把任务放到 prepare 队列，然后立刻返回（handler 不再做注入）。
        """
        # 懒启动该 instance 的 workers
        self._start_workers_for_instance(task.instance_id)

        # 预测总处理时间 = 等待时间 + 处理时间（bs 当前固定 1）
        try:
            know_prepare_ms = await self._estimate_know_prepare_ms(task)
            task.trace["predict_know_prepare_ms"] = int(know_prepare_ms)
        except Exception as e:
            logger.warning("[Predict] rid=%s failed: %s", task.request_id, e)

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
                await self._reserve_ready_task(task, now_s=time.time())
                await q.ready_q.put(task)
                logger.info(
                    "[Queue] enqueue_ready rid=%s instance=%s ready_len=%s",
                    task.request_id, instance_id, q.ready_q.qsize()
                )

            except Exception as e:
                task.error = f"prepare_failed: {e}"
                logger.exception("[Prepare] failed rid=%s fallback no-rag", task.request_id)
                task.trace["ready_enqueue_ms"] = _now_ms()
                await self._reserve_ready_task(task, now_s=time.time())
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
            # 顺序化抓取：避免多个 ready worker 同时抓取引发明显乱序。
            async with self._get_ready_fetch_lock(instance_id):
                now_s = time.time()
                last_fetch_s = self._ready_last_fetch_ts_s.get(instance_id, 0.0)
                wait_s = self._READY_DEQUEUE_INTERVAL_S - (now_s - last_fetch_s)
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                task = await q.ready_q.get()
                self._ready_last_fetch_ts_s[instance_id] = time.time()
            q.active_ready += 1
            try:
                task.trace["ready_dequeue_ms"] = _now_ms()
                task.trace["ready_worker_idx"] = worker_idx
                await self._wait_dispatch_turn(task, instance_id)

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
                            task.trace["ttft_observable"] = 1 if use_chunked else 0
                            if use_chunked:
                                # 实测时延拆分（从 proxy 入队时刻开始）：
                                # actual_total = proxy_enqueue -> first_token
                                # actual_know_prepare = proxy_enqueue -> ready_enqueue
                                # actual_ready_queue  = ready_enqueue -> forward_start
                                # actual_vllm_internal = forward_start -> first_token
                                first_ms = task.trace.get("first_token_ms")
                                enqueue_ms = task.trace.get("proxy_enqueue_ms")
                                ready_enqueue_ms = task.trace.get("ready_enqueue_ms")
                                fwd_start_ms = task.trace.get("forward_start_ms")

                                if isinstance(first_ms, int) and isinstance(enqueue_ms, int):
                                    task.trace["actual_total_ms"] = max(0, first_ms - enqueue_ms)
                                if isinstance(ready_enqueue_ms, int) and isinstance(enqueue_ms, int):
                                    task.trace["actual_know_prepare_ms"] = max(0, ready_enqueue_ms - enqueue_ms)
                                if isinstance(fwd_start_ms, int) and isinstance(ready_enqueue_ms, int):
                                    task.trace["actual_ready_queue_ms"] = max(0, fwd_start_ms - ready_enqueue_ms)
                                if isinstance(first_ms, int) and isinstance(fwd_start_ms, int):
                                    task.trace["actual_vllm_internal_ms"] = max(0, first_ms - fwd_start_ms)
                                    # compatibility field
                                    task.trace["actual_compute_ms"] = task.trace["actual_vllm_internal_ms"]
                                await self._mark_task_first_token_and_recompute(task, instance_id)
                                pred_total_ms = task.trace.get("predict_total_ms")
                                actual_total_ms = task.trace.get("actual_total_ms")
                                if isinstance(pred_total_ms, int) and isinstance(actual_total_ms, int):
                                    task.trace["predict_error_ms"] = int(actual_total_ms - pred_total_ms)

                                logger.info(
                                    "[Timing] rid=%s instance=%s "
                                    "pred(total/know_prepare/queue_wait/vllm_internal)=%s/%s/%s/%s ms "
                                    "actual(total/know_prepare/ready_queue/vllm_internal)=%s/%s/%s/%s ms "
                                    "predict_error=%s ms",
                                    task.request_id,
                                    instance_id,
                                    task.trace.get("predict_total_ms"),
                                    task.trace.get("predict_know_prepare_ms"),
                                    task.trace.get("predict_queue_wait_ms"),
                                    task.trace.get("predict_vllm_internal_ms"),
                                    task.trace.get("actual_total_ms"),
                                    task.trace.get("actual_know_prepare_ms"),
                                    task.trace.get("actual_ready_queue_ms"),
                                    task.trace.get("actual_vllm_internal_ms"),
                                    task.trace.get("predict_error_ms"),
                                )
                            else:
                                logger.info(
                                    "[Timing] rid=%s instance=%s skip_correction=non_stream_response",
                                    task.request_id,
                                    instance_id,
                                )
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
