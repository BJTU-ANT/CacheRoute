# scheduler/strategy/cacheroute.py
from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from .base import ProxySelectionStrategy


class CacheRouteStrategy(ProxySelectionStrategy):
    """
    CacheRoute 调度策略的第一阶段实现。

    当前版本只做最小可用能力：
    1) 基于 request 的 Knowledge_List，在每个 KDN 的知识元数据索引里判断
       - 是否能完整提供请求所需知识文本；
       - 该 KDN 上 kv_ready 覆盖的总长度。
    2) KDN 选择采用“词典序规则”，避免引入加权打分：
       - 优先完整文本覆盖；
       - 优先非过载 KDN；
       - 再比 kv_ready 覆盖长度；
       - 最后比轻负载与稳定顺序。
    3) Proxy 选择先保持保守：优先当前 inflight 最小者，避免一次性把
       拓扑/历史偏好也同时引入。后续版本再把 KDN 锚点拓扑和知识亲和性接进来。

    备注：
    - 这里不依赖 Injection_type 做模式分支；正式策略默认先满足文本可服务性，
      再在可服务候选中偏向 KVCache 覆盖更高的 KDN。
    - request_ctx / kdn_knowledge_index 由 scheduler 内部准备。
    """

    name: str = "cacheroute"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proxy_cursor = 0
        # 第一阶段参数：使用阈值规则（非加权）判断 KDN 过载。
        # 值为 0 表示不启用对应阈值。
        self._kdn_qps_overload_th = float(os.environ.get("SCHEDULER_CACHEROUTE_KDN_QPS_OVERLOAD_TH", "0").strip() or 0)
        self._kdn_items_overload_th = int(os.environ.get("SCHEDULER_CACHEROUTE_KDN_ITEMS_OVERLOAD_TH", "0").strip() or 0)
        self._kdn_pending_overload_th = int(os.environ.get("SCHEDULER_CACHEROUTE_KDN_PENDING_OVERLOAD_TH", "0").strip() or 0)
        # 记录最近一次决策快照，供 /debug/strategy 读取。
        self._last_decision: Dict[str, Any] = {}

    @staticmethod
    def _kdn_addr(kdn: Dict[str, Any]) -> str:
        return f"kdn://{kdn['host']}:{int(kdn['port'])}"

    @staticmethod
    def _proxy_inflight(proxy: Dict[str, Any]) -> int:
        return int(proxy.get('inflight', 0) or 0)

    @staticmethod
    def _proxy_qps(proxy: Dict[str, Any]) -> float:
        return float(proxy.get('qps_1m', 0.0) or 0.0)

    @staticmethod
    def _proxy_gpu(proxy: Dict[str, Any]) -> float:
        return float(proxy.get('gpu_util', 0.0) or 0.0)

    @staticmethod
    def _kdn_items(kdn: Dict[str, Any]) -> int:
        return int(kdn.get('items', 0) or 0)

    @staticmethod
    def _kdn_qps(kdn: Dict[str, Any]) -> float:
        return float(kdn.get('qps_1m', 0.0) or 0.0)

    @staticmethod
    def _is_overloaded(kdn: Dict[str, Any]) -> bool:
        """向后兼容：保留 meta.overloaded 显式覆盖。"""
        meta = kdn.get('meta') or {}
        return bool(meta.get('overloaded', False))

    def _is_overloaded_by_threshold(self, kdn: Dict[str, Any]) -> bool:
        """
        第一阶段过载判定（非加权）：
        1) meta.overloaded 显式为 True -> 过载；
        2) 若配置了阈值，按 qps/items/meta.pending_transfers 逐条判定。
        """
        if self._is_overloaded(kdn):
            return True

        if self._kdn_qps_overload_th > 0 and self._kdn_qps(kdn) >= self._kdn_qps_overload_th:
            return True

        if self._kdn_items_overload_th > 0 and self._kdn_items(kdn) >= self._kdn_items_overload_th:
            return True

        meta = kdn.get("meta") or {}
        pending = int(meta.get("pending_transfers", 0) or 0)
        if self._kdn_pending_overload_th > 0 and pending >= self._kdn_pending_overload_th:
            return True

        return False

    def _select_kdn(
        self,
        kdns: List[Dict[str, Any]],
        knowledge_list: List[str],
        knowledge_index: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        if not kdns:
            return None

        # 没有知识需求时，退化为最轻负载 KDN，避免复杂逻辑干扰原有流程。
        if not knowledge_list:
            ranked = sorted(
                kdns,
                key=lambda k: (
                    self._is_overloaded(k),
                    self._kdn_qps(k),
                    self._kdn_items(k),
                    str(k.get('kdn_id', '')),
                ),
            )
            return ranked[0] if ranked else None

        ranked: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []
        decision_rows: List[Dict[str, Any]] = []
        for kdn in kdns:
            kdn_id = str(kdn.get('kdn_id') or '')
            kdn_addr = self._kdn_addr(kdn)

            # 支持两种索引键：kdn_id 优先，其次 kdn://host:port，方便后续平滑过渡。
            meta_by_kid = knowledge_index.get(kdn_id) or knowledge_index.get(kdn_addr) or {}

            text_full = True
            kv_cover_len = 0
            missing_count = 0
            for kid in knowledge_list:
                item = meta_by_kid.get(kid)
                if item is None:
                    text_full = False
                    missing_count += 1
                    continue
                if int(item.get('kv_ready', 0) or 0) == 1:
                    kv_cover_len += int(item.get('length', 0) or 0)

            # 词典序：
            # 1. 完整文本覆盖优先
            # 2. 非过载优先
            # 3. KVCache 覆盖长度越大越优
            # 4. 缺失知识数越少越优（作为非完整覆盖场景的回退）
            # 5. QPS / items 越小越优
            # 6. kdn_id 稳定打破并列
            key = (
                0 if text_full else 1,
                0 if not self._is_overloaded_by_threshold(kdn) else 1,
                -kv_cover_len,
                missing_count,
                self._kdn_qps(kdn),
                self._kdn_items(kdn),
                kdn_id,
            )
            ranked.append((key, kdn))
            decision_rows.append(
                {
                    "kdn_id": kdn_id,
                    "text_full": text_full,
                    "overloaded": self._is_overloaded_by_threshold(kdn),
                    "kv_cover_len": kv_cover_len,
                    "missing_count": missing_count,
                    "qps_1m": self._kdn_qps(kdn),
                    "items": self._kdn_items(kdn),
                }
            )

        ranked.sort(key=lambda x: x[0])
        chosen = ranked[0][1] if ranked else None
        with self._lock:
            self._last_decision["kdn_candidates"] = decision_rows
            self._last_decision["chosen_kdn_id"] = str((chosen or {}).get("kdn_id", ""))
        return chosen

    def _select_proxy(self, proxies: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not proxies:
            return None

        # 第一阶段保持简单：
        # - 优先 inflight 最小
        # - 再比 qps_1m / gpu_util
        # - 最后使用本地 cursor 做稳定轮换，避免完全固定在一个 proxy 上
        ranked = sorted(
            proxies,
            key=lambda p: (
                self._proxy_inflight(p),
                self._proxy_qps(p),
                self._proxy_gpu(p),
                str(p.get('proxy_id', '')),
            ),
        )
        best = ranked[0]
        best_key = (
            self._proxy_inflight(best),
            self._proxy_qps(best),
            self._proxy_gpu(best),
        )
        tied = [p for p in ranked if (
            self._proxy_inflight(p),
            self._proxy_qps(p),
            self._proxy_gpu(p),
        ) == best_key]

        if len(tied) == 1:
            with self._lock:
                self._last_decision["proxy_candidates"] = [
                    {
                        "proxy_id": str(p.get("proxy_id", "")),
                        "inflight": self._proxy_inflight(p),
                        "qps_1m": self._proxy_qps(p),
                        "gpu_util": self._proxy_gpu(p),
                    }
                    for p in ranked
                ]
                self._last_decision["chosen_proxy_id"] = str(best.get("proxy_id", ""))
            return best

        with self._lock:
            idx = self._proxy_cursor % len(tied)
            self._proxy_cursor += 1
            chosen = tied[idx]
            self._last_decision["proxy_candidates"] = [
                {
                    "proxy_id": str(p.get("proxy_id", "")),
                    "inflight": self._proxy_inflight(p),
                    "qps_1m": self._proxy_qps(p),
                    "gpu_util": self._proxy_gpu(p),
                }
                for p in ranked
            ]
            self._last_decision["chosen_proxy_id"] = str(chosen.get("proxy_id", ""))
            return chosen

    def get_debug_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._last_decision)

    def select(
        self,
        kdns: List[Dict[str, Any]],
        proxies: List[Dict[str, Any]],
        payload: Dict[str, Any],
        url_path: str,
        user_addr: str,
        request_ctx: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        ctx = request_ctx or {}
        knowledge_list = [str(x).strip().lower() for x in (ctx.get('knowledge_list') or []) if str(x).strip()]
        knowledge_index = ctx.get('kdn_knowledge_index') or {}

        chosen_kdn = self._select_kdn(kdns=kdns, knowledge_list=knowledge_list, knowledge_index=knowledge_index)
        chosen_proxy = self._select_proxy(proxies=proxies)
        return chosen_kdn, chosen_proxy
