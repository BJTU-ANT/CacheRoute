# scheduler/strategy/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


class ProxySelectionStrategy(ABC):
    """
    统一路由策略接口：给一组候选 KDNs + Proxies + 当前 request，
    返回 (chosen_kdn, chosen_proxy)。
    后续扩展：可以在这里加更多上下文（如历史统计、请求类别、KV信息等）
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
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        raise NotImplementedError
