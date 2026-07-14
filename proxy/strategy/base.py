# proxy/strategy/base.py
"""Defines proxy-side instance selection strategy interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional, Protocol, Any


class InstanceLike(Protocol):
    instance_id: str
    host: str
    port: int
    weight: float  # reserved; RR does not use it yet


class BaseInstanceStrategy(ABC):
    """
    Base class for proxy-side Instance selection strategies.

    Constraints:
    - Input: current live instance list provided by InstancePool.list(include_dead=False)
    - Output: the selected instance, including host/port and similar fields
    - Does not directly depend on FastAPI/request; upper layers can pass req_obj as a hint
    """
    name: str = "base"

    @abstractmethod
    def select(self, instances: List[InstanceLike], hint: Optional[Any] = None) -> InstanceLike:
        raise NotImplementedError
