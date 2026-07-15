# proxy/queue/instance_queues.py
"""Defines per-instance proxy queues and concurrency limits for prepare and ready phases."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict

from .task import ProxyTask


@dataclass
class InstanceQueues:
    """
    Per-instance local queues and concurrency controls:
    - prepare_q: knowledge preparation phase
    - ready_q: knowledge is ready and the task is waiting for inference forwarding
    - prepare_sem: limits prepare/inject concurrency on the same instance
    """
    prepare_q: "asyncio.Queue[ProxyTask]" = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    ready_q: "asyncio.Queue[ProxyTask]" = field(default_factory=lambda: asyncio.Queue(maxsize=256))

    # PerInstanceQueueMap injects the concrete concurrency at runtime
    prepare_sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))

    # Only used for observability
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

    def snapshot_depths(self) -> Dict[str, Dict[str, int]]:
        """Return current per-instance queue depths for coarse pool-level observability."""
        out: Dict[str, Dict[str, int]] = {}
        for instance_id, queues in self._m.items():
            out[instance_id] = {
                "prepare_queue_depth": int(queues.prepare_q.qsize()),
                "ready_queue_depth": int(queues.ready_q.qsize()),
                "active_prepare": int(queues.active_prepare),
                "active_ready": int(queues.active_ready),
            }
        return out

    @property
    def ready_concurrency_per_instance(self) -> int:
        return self._ready_concurrency_per_instance