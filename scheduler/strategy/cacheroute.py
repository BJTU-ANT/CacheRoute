# scheduler/strategy/cacheroute.py
from __future__ import annotations

import os
import threading
import logging
from typing import Any, Dict, List, Optional, Tuple

from .base import ProxySelectionStrategy

logger = logging.getLogger("scheduler.strategy.cacheroute")


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
    3) Proxy 选择（第二阶段）升级为“非加权词典序”：
       - 先在 chosen_kdn 锚点下选择拓扑最优组（带宽层级优先，时延层级其次）；
       - 再做负载安全窗口过滤（避免单机过载）；
       - 再比较知识历史偏好（倾向同一 LLM 连续处理相近知识）；
       - 最后按当前负载 + 稳定轮转打破并列。

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
        self._kdn_active_overload_th = int(os.environ.get("SCHEDULER_CACHEROUTE_KDN_ACTIVE_OVERLOAD_TH", "0").strip() or 0)
        self._kdn_queue_ms_overload_th = float(os.environ.get("SCHEDULER_CACHEROUTE_KDN_QUEUE_MS_OVERLOAD_TH", "0").strip() or 0.0)
        # Proxy 侧第二阶段参数：负载安全窗口（非加权过滤）
        self._proxy_inflight_delta = int(os.environ.get("SCHEDULER_CACHEROUTE_PROXY_INFLIGHT_DELTA", "2").strip() or 0)
        self._proxy_gpu_delta = float(os.environ.get("SCHEDULER_CACHEROUTE_PROXY_GPU_DELTA", "0").strip() or 0.0)
        # 历史偏好衰减（每次选择后应用），默认 0.9
        self._affinity_decay = float(os.environ.get("SCHEDULER_CACHEROUTE_AFFINITY_DECAY", "0.9").strip() or 0.9)
        # 每个 proxy 只保留最近最有价值的 top-k kid，避免状态无限增长
        self._affinity_topk = int(os.environ.get("SCHEDULER_CACHEROUTE_AFFINITY_TOPK", "256").strip() or 256)
        # 输出简洁的一行决策日志，默认开启；设为 0 可关闭。
        self._log_decision = bool(int(os.environ.get("SCHEDULER_CACHEROUTE_LOG_DECISION", "1").strip() or 0))
        # 记录最近一次决策快照，供 /debug/strategy 读取。
        self._last_decision: Dict[str, Any] = {}
        # 粗粒度知识历史偏好：proxy_id -> kid -> decayed_score
        self._proxy_kid_affinity: Dict[str, Dict[str, float]] = {}
        # v0.1.7: 策略计数器（便于实验比较，不参与调度）
        self._counters: Dict[str, int] = {
            "requests_total": 0,
            "kdn_overload_filtered": 0,
            "proxy_topology_hits": 0,
            "proxy_loadsafe_filtered": 0,
            "proxy_affinity_hits": 0,
        }

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

    @staticmethod
    def _topology_tiers_for_proxy(proxy: Dict[str, Any], chosen_kdn: Dict[str, Any]) -> Tuple[int, int]:
        """
        从 proxy.meta.kdn_links 中读取 chosen_kdn 的拓扑分层：
        - bandwidth_tier: 越大越好（默认 0）
        - latency_tier: 越小越好（默认 999）
        支持 key 为 kdn_id 或 kdn://host:port。
        """
        meta = proxy.get("meta") or {}
        links = meta.get("kdn_links") or {}
        if not isinstance(links, dict):
            return 0, 999

        kdn_id = str(chosen_kdn.get("kdn_id") or "")
        kdn_addr = f"kdn://{chosen_kdn.get('host')}:{int(chosen_kdn.get('port') or 0)}"
        item = links.get(kdn_id) or links.get(kdn_addr) or {}
        if not isinstance(item, dict):
            return 0, 999
        bw_tier = int(item.get("bandwidth_tier", 0) or 0)
        lat_tier = int(item.get("latency_tier", 999) or 999)
        return bw_tier, lat_tier

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
        pending = int(kdn.get("pending_transfers", meta.get("pending_transfers", 0)) or 0)
        active = int(kdn.get("active_transfers", meta.get("active_transfers", 0)) or 0)
        queue_ms = float(kdn.get("network_queue_ms_ema", meta.get("network_queue_ms_ema", 0.0)) or 0.0)
        if self._kdn_pending_overload_th > 0 and pending >= self._kdn_pending_overload_th:
            return True
        if self._kdn_active_overload_th > 0 and active >= self._kdn_active_overload_th:
            return True
        if self._kdn_queue_ms_overload_th > 0 and queue_ms >= self._kdn_queue_ms_overload_th:
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
                    self._is_overloaded_by_threshold(k),
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
        overload_cnt = sum(1 for d in decision_rows if bool(d.get("overloaded")))
        with self._lock:
            self._last_decision["kdn_candidates"] = decision_rows
            self._last_decision["chosen_kdn_id"] = str((chosen or {}).get("kdn_id", ""))
            self._counters["kdn_overload_filtered"] += overload_cnt
        return chosen

    def _affinity_score(
        self,
        proxy_id: str,
        knowledge_list: List[str],
        length_by_kid: Dict[str, int],
    ) -> float:
        table = self._proxy_kid_affinity.get(proxy_id) or {}
        if not table or not knowledge_list:
            return 0.0
        score = 0.0
        for kid in knowledge_list:
            w = float(length_by_kid.get(kid, 1) or 1)
            score += float(table.get(kid, 0.0)) * w
        return score

    def _select_proxy(
        self,
        proxies: List[Dict[str, Any]],
        chosen_kdn: Optional[Dict[str, Any]],
        knowledge_list: List[str],
        length_by_kid: Dict[str, int],
    ) -> Optional[Dict[str, Any]]:
        if not proxies:
            return None
        # Step1: 以 chosen_kdn 为锚点选拓扑最优组（无 chosen_kdn 时退化为全量候选）
        topology_rows: List[Tuple[Tuple[int, int], Dict[str, Any]]] = []
        for p in proxies:
            bw_tier, lat_tier = self._topology_tiers_for_proxy(p, chosen_kdn) if chosen_kdn else (0, 999)
            # 词典序：带宽 tier 越大越好，时延 tier 越小越好
            topology_rows.append(((-bw_tier, lat_tier), p))
        topology_rows.sort(key=lambda x: x[0])
        best_topo = topology_rows[0][0]
        topo_group = [p for key, p in topology_rows if key == best_topo]
        if len(topo_group) < len(proxies):
            with self._lock:
                self._counters["proxy_topology_hits"] += 1

        # Step2: 负载安全窗口过滤（避免把知识亲和性放在过载 proxy 上）
        min_inflight = min(self._proxy_inflight(p) for p in topo_group)
        safe_group = [
            p for p in topo_group
            if self._proxy_inflight(p) <= (min_inflight + max(0, self._proxy_inflight_delta))
        ]
        if len(safe_group) < len(topo_group):
            with self._lock:
                self._counters["proxy_loadsafe_filtered"] += 1
        if self._proxy_gpu_delta > 0 and safe_group:
            min_gpu = min(self._proxy_gpu(p) for p in safe_group)
            safe_group = [
                p for p in safe_group
                if self._proxy_gpu(p) <= (min_gpu + self._proxy_gpu_delta)
            ] or safe_group

        # Step3: 在安全窗口内按知识历史偏好选最优
        affinity_rows: List[Tuple[float, Dict[str, Any]]] = []
        for p in safe_group:
            pid = str(p.get("proxy_id", ""))
            affinity_rows.append((self._affinity_score(pid, knowledge_list, length_by_kid), p))
        max_aff = max((x[0] for x in affinity_rows), default=0.0)
        aff_group = [p for sc, p in affinity_rows if sc == max_aff]
        if max_aff > 0:
            with self._lock:
                self._counters["proxy_affinity_hits"] += 1

        # Step4: 按当前负载打破并列，再用 cursor 稳定轮换
        ranked = sorted(
            aff_group,
            key=lambda p: (
                self._proxy_inflight(p),
                self._proxy_qps(p),
                self._proxy_gpu(p),
                str(p.get('proxy_id', '')),
            ),
        )
        best = ranked[0]
        best_key = (self._proxy_inflight(best), self._proxy_qps(best), self._proxy_gpu(best))
        tied = [p for p in ranked if (self._proxy_inflight(p), self._proxy_qps(p), self._proxy_gpu(p)) == best_key]

        # 更新亲和性：选中 proxy 对本次知识集合增益；其他 proxy 衰减
        chosen = best
        with self._lock:
            if len(tied) > 1:
                idx = self._proxy_cursor % len(tied)
                self._proxy_cursor += 1
                chosen = tied[idx]

            for p in proxies:
                pid = str(p.get("proxy_id", ""))
                table = self._proxy_kid_affinity.setdefault(pid, {})
                if not table:
                    continue
                for kid in list(table.keys()):
                    table[kid] = float(table[kid]) * self._affinity_decay
                    if table[kid] < 1e-6:
                        table.pop(kid, None)

            chosen_pid = str(chosen.get("proxy_id", ""))
            chosen_table = self._proxy_kid_affinity.setdefault(chosen_pid, {})
            for kid in knowledge_list:
                chosen_table[kid] = float(chosen_table.get(kid, 0.0)) + 1.0
            # 只保留 top-k，避免长期实验状态膨胀
            if self._affinity_topk > 0 and len(chosen_table) > self._affinity_topk:
                kept = sorted(chosen_table.items(), key=lambda kv: kv[1], reverse=True)[: self._affinity_topk]
                self._proxy_kid_affinity[chosen_pid] = dict(kept)

            self._last_decision["proxy_candidates"] = [
                {
                    "proxy_id": str(p.get("proxy_id", "")),
                    "inflight": self._proxy_inflight(p),
                    "qps_1m": self._proxy_qps(p),
                    "gpu_util": self._proxy_gpu(p),
                    "topology_tier": {
                        "bandwidth_tier": self._topology_tiers_for_proxy(p, chosen_kdn)[0] if chosen_kdn else 0,
                        "latency_tier": self._topology_tiers_for_proxy(p, chosen_kdn)[1] if chosen_kdn else 999,
                    },
                    "affinity_score": self._affinity_score(str(p.get("proxy_id", "")), knowledge_list, length_by_kid),
                }
                for p in proxies
            ]
            self._last_decision["chosen_proxy_id"] = chosen_pid
        return chosen

    def get_debug_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            out = dict(self._last_decision)
            out["counters"] = dict(self._counters)
            return out

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
        # 统一使用知识索引中的 length，作为亲和性打分的长度权重来源
        length_by_kid: Dict[str, int] = {}
        for _kdn_id, by_kid in (knowledge_index or {}).items():
            if not isinstance(by_kid, dict):
                continue
            for kid, meta in by_kid.items():
                if not isinstance(meta, dict):
                    continue
                if kid not in length_by_kid:
                    length_by_kid[kid] = int(meta.get("length", 0) or 0)

        chosen_kdn = self._select_kdn(kdns=kdns, knowledge_list=knowledge_list, knowledge_index=knowledge_index)
        chosen_proxy = self._select_proxy(
            proxies=proxies,
            chosen_kdn=chosen_kdn,
            knowledge_list=knowledge_list,
            length_by_kid=length_by_kid,
        )
        with self._lock:
            self._counters["requests_total"] += 1
        if self._log_decision:
            try:
                logger.info(
                    "[CacheRoute] req=%s kdn=%s proxy=%s kids=%d kdn_candidates=%d proxy_candidates=%d",
                    str(ctx.get("request_id", "")),
                    str((chosen_kdn or {}).get("kdn_id", "")),
                    str((chosen_proxy or {}).get("proxy_id", "")),
                    len(knowledge_list),
                    len(self._last_decision.get("kdn_candidates", []) or []),
                    len(self._last_decision.get("proxy_candidates", []) or []),
                )
            except Exception:
                # 日志失败不应影响调度路径
                pass
        return chosen_kdn, chosen_proxy
