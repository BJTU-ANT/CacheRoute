# proxy/strategy/round_robin.py
"""Implements round-robin instance selection for the proxy."""
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
            # idx only increases here to avoid duplicates or inconsistent skips under concurrent requests
            i = self._idx % len(instances)
            self._idx += 1
            return instances[i]
