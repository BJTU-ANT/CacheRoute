# scheduler/strategy/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


class ProxySelectionStrategy(ABC):
    """
    统一路由策略接口：给一组候选 KDNs + Proxies + 当前 request，
    返回 (chosen_kdn, chosen_proxy)。

    request_ctx 采用轻量上下文形式，用来给策略传递已经在 scheduler
    内部计算完成的信息（例如 Knowledge_List、知识长度、每个 KDN 的知识元数据索引等），
    避免策略层重复做 embedding 检索。
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
