# proxy/queue/instance_queues.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict

from .task import ProxyTask


@dataclass
class InstanceQueues:
    """
    每个 instance 两个队列：
    - prepare_q：知识准备阶段
    - ready_q：知识已就绪，等待推理转发阶段
    """
    prepare_q: "asyncio.Queue[ProxyTask]" = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    ready_q: "asyncio.Queue[ProxyTask]" = field(default_factory=lambda: asyncio.Queue(maxsize=256))


class PerInstanceQueueMap:
    def __init__(self) -> None:
        self._m: Dict[str, InstanceQueues] = {}

    def get(self, instance_id: str) -> InstanceQueues:
        if instance_id not in self._m:
            self._m[instance_id] = InstanceQueues()
        return self._m[instance_id]