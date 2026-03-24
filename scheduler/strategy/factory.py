# scheduler/strategy/factory.py
from __future__ import annotations

from typing import Dict

from .base import ProxySelectionStrategy
from .round_robin import RoundRobinStrategy
from .cacheroute import CacheRouteStrategy


_STRATEGIES: Dict[str, type[ProxySelectionStrategy]] = {
    "round_robin": RoundRobinStrategy,
    "cacheroute": CacheRouteStrategy,
}


def create_strategy(name: str) -> ProxySelectionStrategy:
    key = (name or "").strip().lower()
    if not key:
        key = "round_robin"
    if key not in _STRATEGIES:
        raise ValueError(f"Unknown strategy: {name!r}. Available={list(_STRATEGIES.keys())}")
    return _STRATEGIES[key]()
