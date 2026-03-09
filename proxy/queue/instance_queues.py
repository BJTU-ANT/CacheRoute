# proxy/queue/instance_queues.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict

from .task import ProxyTask


@dataclass
class InstanceQueues:
    """
    每个 instance 的本地队列与并发控制：
    - prepare_q：知识准备阶段
    - ready_q：知识已就绪，等待推理转发阶段
    - prepare_sem：限制同一 instance 上 prepare/inject 的并发数
    """
    prepare_q: "asyncio.Queue[ProxyTask]" = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    ready_q: "asyncio.Queue[ProxyTask]" = field(default_factory=lambda: asyncio.Queue(maxsize=256))

    # 运行时由 PerInstanceQueueMap 注入具体并发度
    prepare_sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))

    # 仅用于观测
    active_prepare: int = 0
    active_ready: int = 0


class PerInstanceQueueMap:
    def __init__(
            self,
            prepare_concurrency_per_instance: int = 8,
            ready_concurrency_per_instance: int = 8,
    ) -> None:
        self._m: Dict[str, InstanceQueues] = {}
        self._prepare_concurrency_per_instance = max(1, int(prepare_concurrency_per_instance))
        self._ready_concurrency_per_instance = max(1, int(ready_concurrency_per_instance))

    def get(self, instance_id: str) -> InstanceQueues:
        if instance_id not in self._m:
            self._m[instance_id] = InstanceQueues(
                prepare_sem=asyncio.Semaphore(self._prepare_concurrency_per_instance)
            )
        return self._m[instance_id]

    @property
    def ready_concurrency_per_instance(self) -> int:
        return self._ready_concurrency_per_instance