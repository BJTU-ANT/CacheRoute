# proxy/strategy/factory.py
from __future__ import annotations

from .base import BaseInstanceStrategy
from .round_robin import RoundRobinStrategy


def build_instance_strategy(name: str) -> BaseInstanceStrategy:
    n = (name or "").strip().lower()
    if n in ("rr", "round_robin", "round-robin"):
        return RoundRobinStrategy()
    raise ValueError(f"unknown instance strategy: {name}")
