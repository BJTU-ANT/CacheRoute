# proxy/strategy/least_load.py
"""Implements least-load instance selection for the proxy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .base import BaseInstanceStrategy, InstanceLike
from .round_robin import RoundRobinStrategy


@dataclass(frozen=True)
class LeastLoadWeights:
    """Conservative queue-aware weights for least-load scoring."""

    active_prepare: float = 1.0
    active_ready: float = 1.0
    prepare_queue_depth: float = 0.25
    ready_queue_depth: float = 0.25
    qps_1m: float = 0.0


class LeastLoadStrategy(BaseInstanceStrategy):
    """
    Select the Instance with the lowest known weighted load.

    ``load.inflight`` remains the primary signal with implicit weight 1.0.
    Queue hints are added when the Proxy queue manager provides per-Instance
    measurements. Missing metrics stay unknown rather than becoming measured
    zeros. If no candidate has any measurable score component, selection falls
    back to round-robin.
    """

    name = "least_load"

    def __init__(self, weights: Optional[LeastLoadWeights] = None) -> None:
        self._rr = RoundRobinStrategy()
        self.weights = weights or LeastLoadWeights()

    def select(self, instances: List[InstanceLike], hint: Optional[Any] = None) -> InstanceLike:
        if not instances:
            raise RuntimeError("no instances")

        scored = [(it, self.compute_score(it, hint)) for it in instances]
        known_scores = [(it, score) for it, score in scored if score["total"] is not None]
        if not known_scores:
            return self._rr.select(instances)

        min_score = min(score["total"] for _, score in known_scores)
        ties = [it for it, score in known_scores if score["total"] == min_score]
        return self._rr.select(ties)

    def compute_score(self, instance: InstanceLike, hint: Optional[Any] = None) -> Dict[str, Any]:
        """Compute weighted score details for one Instance without mutating state."""
        components: Dict[str, Optional[float]] = {}
        sources: Dict[str, str] = {}

        inflight = self._inflight(instance)
        if inflight is not None:
            components["inflight"] = float(inflight)
            sources["inflight"] = "proxy_lifecycle_counter"

        queue_item = self._queue_info(instance, hint)
        queue_source = "proxy_queue_manager" if queue_item is not None else "unavailable"
        if queue_item is not None:
            for key in ("active_prepare", "active_ready", "prepare_queue_depth", "ready_queue_depth"):
                value = self._number_from_mapping(queue_item, key)
                if value is not None:
                    components[key] = value
                    sources[key] = "proxy_queue_manager"

        qps_1m = self._qps_1m(instance)
        if qps_1m is not None:
            components["qps_1m"] = qps_1m
            sources["qps_1m"] = "instance_heartbeat"

        weighted_components = self._weighted_components(components)
        total = sum(weighted_components.values()) if weighted_components else None
        return {
            "total": total,
            "components": components,
            "weighted_components": weighted_components,
            "source": sources,
            "queue_source": queue_source,
            "weights": self.weights_dict(),
        }

    def weights_dict(self) -> Dict[str, float]:
        return {
            "inflight": 1.0,
            "active_prepare": self.weights.active_prepare,
            "active_ready": self.weights.active_ready,
            "prepare_queue_depth": self.weights.prepare_queue_depth,
            "ready_queue_depth": self.weights.ready_queue_depth,
            "qps_1m": self.weights.qps_1m,
        }

    def _weighted_components(self, components: Dict[str, Optional[float]]) -> Dict[str, float]:
        weights = self.weights_dict()
        weighted: Dict[str, float] = {}
        for key, value in components.items():
            if value is None:
                continue
            weight = weights.get(key)
            if weight is None or weight == 0.0:
                continue
            weighted[key] = float(value) * float(weight)
        return weighted

    @staticmethod
    def _queue_info(instance: InstanceLike, hint: Optional[Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(hint, dict):
            return None
        queue_depths = hint.get("queue_depths")
        if not isinstance(queue_depths, dict):
            return None
        per_instance = queue_depths.get("per_instance")
        if not isinstance(per_instance, dict):
            return None
        queue_item = per_instance.get(getattr(instance, "instance_id", None))
        return queue_item if isinstance(queue_item, dict) else None

    @staticmethod
    def _number_from_mapping(mapping: Dict[str, Any], key: str) -> Optional[float]:
        value = mapping.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

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
