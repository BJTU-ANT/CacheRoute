# scheduler/strategy/base.py
"""Defines Scheduler-side KDN/proxy selection strategy interfaces."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


class ProxySelectionStrategy(ABC):
    """
    Unified routing strategy interface: given candidate KDNs, proxies, and the current request,
    return (chosen_kdn, chosen_proxy).

    request_ctx is a lightweight context used to pass information that the scheduler has already computed
    such as Knowledge_List, knowledge length, and each KDN knowledge metadata index,
    so the strategy layer does not repeat embedding retrieval.
    """

    name: str = "base"

    @abstractmethod
    def select(
            self,
            kdns: List[Dict[str, Any]],
            proxies: List[Dict[str, Any]],
            payload: Dict[str, Any],
            url_path: str,
            user_addr: str,
            request_ctx: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        raise NotImplementedError
