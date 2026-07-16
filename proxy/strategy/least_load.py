# proxy/strategy/least_load.py
"""Implements least-load instance selection for the proxy."""
from __future__ import annotations

from typing import Any, List, Optional

from .base import BaseInstanceStrategy, InstanceLike
from .round_robin import RoundRobinStrategy


class LeastLoadStrategy(BaseInstanceStrategy):
    """
    Select the Instance with the lowest known load.

    The strategy prefers known ``load.inflight`` values. It uses known
    ``load.qps_1m`` as a secondary signal, without treating missing metrics as
    zero. When no useful load metrics are known, it falls back to round-robin.
    """

    name = "least_load"

    def __init__(self) -> None:
        self._rr = RoundRobinStrategy()

    def select(self, instances: List[InstanceLike], hint: Optional[Any] = None) -> InstanceLike:
        if not instances:
            raise RuntimeError("no instances")

        known_inflight = [it for it in instances if self._inflight(it) is not None]
        if known_inflight:
            min_inflight = min(self._inflight(it) for it in known_inflight)
            inflight_ties = [it for it in known_inflight if self._inflight(it) == min_inflight]
            return self._select_by_qps_or_rr(inflight_ties)

        return self._select_by_qps_or_rr(instances)

    def _select_by_qps_or_rr(self, instances: List[InstanceLike]) -> InstanceLike:
        known_qps = [it for it in instances if self._qps_1m(it) is not None]
        if not known_qps:
            return self._rr.select(instances)

        min_qps = min(self._qps_1m(it) for it in known_qps)
        qps_ties = [it for it in known_qps if self._qps_1m(it) == min_qps]
        return self._rr.select(qps_ties)

    @staticmethod
    def _inflight(instance: InstanceLike) -> Optional[int]:
        load = getattr(instance, "load", None)
        value = getattr(load, "inflight", None)
        return int(value) if value is not None else None

    @staticmethod
    def _qps_1m(instance: InstanceLike) -> Optional[float]:
        load = getattr(instance, "load", None)
        value = getattr(load, "qps_1m", None)
        return float(value) if value is not None else None
