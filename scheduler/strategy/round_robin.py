# scheduler/strategy/round_robin.py
"""Implements round-robin proxy selection for the Scheduler."""
from __future__ import annotations

import threading

from typing import Any, Dict, List, Optional, Tuple
from .base import ProxySelectionStrategy


class RoundRobinStrategy(ProxySelectionStrategy):
    """
    The simplest round-robin:
    - perform modulo rotation over the current live proxy list
    - protect the index with threading.Lock to avoid concurrent requests disrupting order
    - request_ctx is unused by this strategy but kept for a unified interface so the scheduler can call all strategies uniformly
    """

    name: str = "round_robin"

    def __init__(self):
        self._lock = threading.Lock()
        self._kdn_cursor = 0
        self._proxy_cursor = 0

    def select(
        self,
        kdns: List[Dict[str, Any]],
        proxies: List[Dict[str, Any]],
        payload: Dict[str, Any],
        url_path: str,
        user_addr: str,
        request_ctx: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        chosen_kdn = None
        chosen_proxy = None

        with self._lock:
            if kdns:
                ki = self._kdn_cursor % len(kdns)
                self._kdn_cursor += 1
                chosen_kdn = kdns[ki]

            if proxies:
                pi = self._proxy_cursor % len(proxies)
                self._proxy_cursor += 1
                chosen_proxy = proxies[pi]

        return chosen_kdn, chosen_proxy
