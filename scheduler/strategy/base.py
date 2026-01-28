# scheduler/strategy/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class ProxySelectionStrategy(ABC):
    """
    策略接口：给一组候选 proxies + 当前 request，返回一个选中的 proxy。
    后续扩展：可以在这里加更多上下文（如历史统计、请求类别、KV信息等）
    """

    name: str = "base"


    @abstractmethod
    def select(
        self,
        proxies: List[Dict[str, Any]],
        payload: Dict[str, Any],
        url_path: str,
        user_addr: str,
    ) -> Optional[Dict[str, Any]]:

        raise NotImplementedError
