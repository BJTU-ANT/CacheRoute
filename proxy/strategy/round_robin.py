# proxy/strategy/round_robin.py
from __future__ import annotations

import threading
from typing import List, Optional, Any

from .base import BaseInstanceStrategy, InstanceLike


class RoundRobinStrategy(BaseInstanceStrategy):
    name = "round_robin"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idx = 0

    def select(self, instances: List[InstanceLike], hint: Optional[Any] = None) -> InstanceLike:
        if not instances:
            raise RuntimeError("no instances")
        with self._lock:
            # idx 只在这里增长，避免并发请求下重复/跳号不一致
            i = self._idx % len(instances)
            self._idx += 1
            return instances[i]
