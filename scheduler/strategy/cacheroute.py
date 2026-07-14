# scheduler/strategy/cacheroute.py
from __future__ import annotations

import os
import threading
import logging
from typing import Any, Dict, List, Optional, Tuple

from .base import ProxySelectionStrategy
from core import config

logger = logging.getLogger("scheduler.strategy.cacheroute")


class CacheRouteStrategy(ProxySelectionStrategy):
    """
    First-stage implementation of the CacheRoute scheduling strategy.

    The current version implements only the minimum usable capabilities:
    1) Based on the request Knowledge_List, check each KDN knowledge metadata index for
       - whether it can fully provide the knowledge text required by the request;
       - the total length covered by kv_ready on that KDN.
    2) KDN selection uses lexicographic rules to avoid introducing weighted scoring:
       - prefer complete text coverage;
       - prefer non-overloaded KDNs;
       - then compare kv_ready coverage length;
       - finally compare lighter load and stable order.
    3) Proxy selection (stage 2) is upgraded to non-weighted lexicographic ordering:
       - first select the topology-best group anchored at chosen_kdn (bandwidth tier first, latency tier second);
       - then apply load safety-window filtering to avoid single-node overload;
       - then compare knowledge history preference, favoring the same LLM for consecutive similar knowledge;
       - finally break ties by current load plus stable rotation.

    Notes:
    - This does not branch on Injection_type; the formal strategy first satisfies text serviceability by default,
      then prefers KDNs with higher KVCache coverage among serviceable candidates.
    - request_ctx / kdn_knowledge_index are prepared inside the scheduler.
    """

    name: str = "cacheroute"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proxy_cursor = 0
        # Stage-1 parameters: use threshold rules (non-weighted) to determine KDN overload.
        # A value of 0 means the corresponding threshold is disabled.
        self._kdn_qps_overload_th = float(os.environ.get("SCHEDULER_CACHEROUTE_KDN_QPS_OVERLOAD_TH", str(config.SCHEDULER_CACHEROUTE_KDN_QPS_OVERLOAD_TH)).strip() or 0)
        self._kdn_items_overload_th = int(os.environ.get("SCHEDULER_CACHEROUTE_KDN_ITEMS_OVERLOAD_TH", str(config.SCHEDULER_CACHEROUTE_KDN_ITEMS_OVERLOAD_TH)).strip() or 0)
        self._kdn_pending_overload_th = int(os.environ.get("SCHEDULER_CACHEROUTE_KDN_PENDING_OVERLOAD_TH", str(config.SCHEDULER_CACHEROUTE_KDN_PENDING_OVERLOAD_TH)).strip() or 0)
        self._kdn_active_overload_th = int(os.environ.get("SCHEDULER_CACHEROUTE_KDN_ACTIVE_OVERLOAD_TH", str(config.SCHEDULER_CACHEROUTE_KDN_ACTIVE_OVERLOAD_TH)).strip() or 0)
        self._kdn_queue_ms_overload_th = float(os.environ.get("SCHEDULER_CACHEROUTE_KDN_QUEUE_MS_OVERLOAD_TH", str(config.SCHEDULER_CACHEROUTE_KDN_QUEUE_MS_OVERLOAD_TH)).strip() or 0.0)
        # Proxy-side stage-2 parameters: load safety window (non-weighted filtering)
        self._proxy_inflight_delta = int(os.environ.get("SCHEDULER_CACHEROUTE_PROXY_INFLIGHT_DELTA", str(config.SCHEDULER_CACHEROUTE_PROXY_INFLIGHT_DELTA)).strip() or 0)
        self._proxy_gpu_delta = float(os.environ.get("SCHEDULER_CACHEROUTE_PROXY_GPU_DELTA", str(config.SCHEDULER_CACHEROUTE_PROXY_GPU_DELTA)).strip() or 0.0)
        # Step 2 addition: safety window based on load ratio (real_time_inflight/max_inflight); recommended delta range is [0,1]
        self._proxy_load_ratio_delta = float(os.environ.get("SCHEDULER_CACHEROUTE_PROXY_LOAD_RATIO_DELTA", str(config.SCHEDULER_CACHEROUTE_PROXY_LOAD_RATIO_DELTA)).strip() or 0.0)
        # History-preference decay applied after each selection; default is 0.9
        self._affinity_decay = float(os.environ.get("SCHEDULER_CACHEROUTE_AFFINITY_DECAY", str(config.SCHEDULER_CACHEROUTE_AFFINITY_DECAY)).strip() or 0.9)
        # Keep only the most valuable recent top-k kids per proxy to avoid unbounded state growth
        self._affinity_topk = int(os.environ.get("SCHEDULER_CACHEROUTE_AFFINITY_TOPK", str(config.SCHEDULER_CACHEROUTE_AFFINITY_TOPK)).strip() or 256)
        # Output a concise one-line decision log; enabled by default and disabled by setting 0.
        self._log_decision = bool(int(os.environ.get("SCHEDULER_CACHEROUTE_LOG_DECISION", str(config.SCHEDULER_CACHEROUTE_LOG_DECISION)).strip() or 0))
        # Record the latest decision snapshot for /debug/strategy.
        self._last_decision: Dict[str, Any] = {}
        # Coarse-grained knowledge history preference: proxy_id -> kid -> decayed_score
        self._proxy_kid_affinity: Dict[str, Dict[str, float]] = {}
        # v0.1.7: Strategy counters for experiment comparison; not used in scheduling
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
    def _proxy_max_inflight(proxy: Dict[str, Any]) -> int:
        return int(proxy.get('max_inflight', 0) or 0)

    def _proxy_load_ratio(self, proxy: Dict[str, Any]) -> float:
        """
        Real-time load ratio:
          load_ratio = real_time_inflight / max_inflight
        Notes:
        - When max_inflight <= 0, treat it as non-normalizable and return 1.0 conservatively
        - The result is limited to [0, +inf); upper-layer comparison only uses relative size
        """
        max_inflight = self._proxy_max_inflight(proxy)
        if max_inflight <= 0:
            return 1.0
        return max(0.0, float(self._proxy_inflight(proxy)) / float(max_inflight))

    @staticmethod
    def _kdn_items(kdn: Dict[str, Any]) -> int:
        return int(kdn.get('items', 0) or 0)

    @staticmethod
    def _kdn_qps(kdn: Dict[str, Any]) -> float:
        return float(kdn.get('qps_1m', 0.0) or 0.0)

    @staticmethod
    def _is_overloaded(kdn: Dict[str, Any]) -> bool:
        """Backward compatibility: keep explicit meta.overloaded override."""
        meta = kdn.get('meta') or {}
        return bool(meta.get('overloaded', False))

    @staticmethod
    def _topology_link_for_proxy(proxy: Dict[str, Any], chosen_kdn: Dict[str, Any]) -> Tuple[float, float]:
        """
        Read chosen_kdn link information from proxy.meta.kdn_links:
        - bandwidth_mbps: larger is better (default 0.0)
        - latency_ms: smaller is better (default +inf)
        Supports keys as either kdn_id or kdn://host:port.
        """
        meta = proxy.get("meta") or {}
        links = meta.get("kdn_links") or {}
        if not isinstance(links, dict):
            return 0.0, 1e9

        kdn_id = str(chosen_kdn.get("kdn_id") or "")
        kdn_addr = f"kdn://{chosen_kdn.get('host')}:{int(chosen_kdn.get('port') or 0)}"
        item = links.get(kdn_id) or links.get(kdn_addr) or {}
        if not isinstance(item, dict):
            return 0.0, 1e9
        bw_mbps = float(item.get("bandwidth_mbps", 0.0) or 0.0)
        lat_ms_raw = item.get("latency_ms", None)
        lat_ms = float(lat_ms_raw) if lat_ms_raw is not None else 1e9
        return bw_mbps, lat_ms

    def _is_overloaded_by_threshold(self, kdn: Dict[str, Any]) -> bool:
        """
        Stage-1 overload check (non-weighted):
        1) meta.overloaded explicitly True -> overloaded;
        2) if thresholds are configured, check qps/items/meta.pending_transfers one by one.
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

        # When there is no knowledge requirement, fall back to the lightest-load KDN so complex logic does not interfere with the existing flow.
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
        usable_rows: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []
        for kdn in kdns:
            kdn_id = str(kdn.get('kdn_id') or '')
            kdn_addr = self._kdn_addr(kdn)

            # Support two index keys: kdn_id first, then kdn://host:port, enabling a smooth future transition.
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

            # Lexicographic order:
            # 1. prefer complete text coverage
            # 2. prefer non-overloaded resources
            # 3. larger KVCache coverage length is better
            # 4. fewer missing knowledge items is better, as a fallback when coverage is incomplete
            # 5. lower QPS / item count is better
            # 6. use kdn_id for stable tie-breaking
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
            # Step 1 target semantics: usable KDN = complete text coverage + not overloaded
            if text_full and (not self._is_overloaded_by_threshold(kdn)):
                usable_key = (
                    -kv_cover_len,           # larger reusable KV coverage length is better
                    self._kdn_qps(kdn),      # prefer lower load on ties
                    self._kdn_items(kdn),
                    kdn_id,
                )
                usable_rows.append((usable_key, kdn))

        # Step 1: prefer selecting from the usable set; if empty, fall back to the original ordering rule
        if usable_rows:
            usable_rows.sort(key=lambda x: x[0])
            chosen = usable_rows[0][1]
        else:
            ranked.sort(key=lambda x: x[0])
            chosen = ranked[0][1] if ranked else None
        overload_cnt = sum(1 for d in decision_rows if bool(d.get("overloaded")))
        with self._lock:
            self._last_decision["kdn_candidates"] = decision_rows
            self._last_decision["usable_kdn_count"] = len(usable_rows)
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
        # Step 2 semantic alignment:
        #  0) select only among LLM systems reachable from chosen_kdn
        #  1) load safety window (minimum load_ratio + delta)
        #  2) historical knowledge preference
        #  3) better link bandwidth
        #  4) lower load
        if chosen_kdn:
            connectable = [
                p for p in proxies
                if self._topology_link_for_proxy(p, chosen_kdn)[0] > 0.0
            ]
            proxy_pool = connectable if connectable else proxies
        else:
            proxy_pool = proxies

        # Step1: load safety (prefer load_ratio; fall back to inflight window when max_inflight is unavailable)
        min_ratio = min(self._proxy_load_ratio(p) for p in proxy_pool)
        safe_group = [
            p for p in proxy_pool
            if self._proxy_load_ratio(p) <= (min_ratio + max(0.0, self._proxy_load_ratio_delta))
        ]
        if not safe_group:
            min_inflight = min(self._proxy_inflight(p) for p in proxy_pool)
            safe_group = [
                p for p in proxy_pool
                if self._proxy_inflight(p) <= (min_inflight + max(0, self._proxy_inflight_delta))
            ]
        if len(safe_group) < len(proxy_pool):
            with self._lock:
                self._counters["proxy_loadsafe_filtered"] += 1
        if self._proxy_gpu_delta > 0 and safe_group:
            min_gpu = min(self._proxy_gpu(p) for p in safe_group)
            safe_group = [
                p for p in safe_group
                if self._proxy_gpu(p) <= (min_gpu + self._proxy_gpu_delta)
            ] or safe_group

        # Step2: history preference (accumulated kids + decay)
        affinity_rows: List[Tuple[float, Dict[str, Any]]] = []
        for p in safe_group:
            pid = str(p.get("proxy_id", ""))
            affinity_rows.append((self._affinity_score(pid, knowledge_list, length_by_kid), p))
        max_aff = max((x[0] for x in affinity_rows), default=0.0)
        aff_group = [p for sc, p in affinity_rows if sc == max_aff]
        if max_aff > 0:
            with self._lock:
                self._counters["proxy_affinity_hits"] += 1

        # Step3+Step4: better bandwidth -> lower load
        ranked = sorted(
            aff_group,
            key=lambda p: (
                -self._topology_link_for_proxy(p, chosen_kdn)[0] if chosen_kdn else 0.0,
                self._topology_link_for_proxy(p, chosen_kdn)[1] if chosen_kdn else 1e9,
                self._proxy_load_ratio(p),
                self._proxy_inflight(p),
                self._proxy_qps(p),
                self._proxy_gpu(p),
                str(p.get('proxy_id', '')),
            ),
        )
        if len(aff_group) > 1 and chosen_kdn:
            with self._lock:
                self._counters["proxy_topology_hits"] += 1
        best = ranked[0]
        best_key = (
            self._topology_link_for_proxy(best, chosen_kdn)[0] if chosen_kdn else 0.0,
            self._topology_link_for_proxy(best, chosen_kdn)[1] if chosen_kdn else 1e9,
            self._proxy_load_ratio(best),
            self._proxy_inflight(best),
            self._proxy_qps(best),
            self._proxy_gpu(best),
        )
        tied = [
            p for p in ranked
            if (
                self._topology_link_for_proxy(p, chosen_kdn)[0] if chosen_kdn else 0.0,
                self._topology_link_for_proxy(p, chosen_kdn)[1] if chosen_kdn else 1e9,
                self._proxy_load_ratio(p),
                self._proxy_inflight(p),
                self._proxy_qps(p),
                self._proxy_gpu(p),
            ) == best_key
        ]

        # Update affinity: boost the selected proxy for this knowledge set and decay other proxies
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
            # Keep only top-k to avoid state growth in long-running experiments
            if self._affinity_topk > 0 and len(chosen_table) > self._affinity_topk:
                kept = sorted(chosen_table.items(), key=lambda kv: kv[1], reverse=True)[: self._affinity_topk]
                self._proxy_kid_affinity[chosen_pid] = dict(kept)

            self._last_decision["proxy_candidates"] = [
                {
                    "proxy_id": str(p.get("proxy_id", "")),
                    "inflight": self._proxy_inflight(p),
                    "max_inflight": self._proxy_max_inflight(p),
                    "load_ratio": self._proxy_load_ratio(p),
                    "qps_1m": self._proxy_qps(p),
                    "gpu_util": self._proxy_gpu(p),
                    "topology_link": {
                        "bandwidth_mbps": self._topology_link_for_proxy(p, chosen_kdn)[0] if chosen_kdn else 0.0,
                        "latency_ms": self._topology_link_for_proxy(p, chosen_kdn)[1] if chosen_kdn else 1e9,
                    },
                    "affinity_score": self._affinity_score(str(p.get("proxy_id", "")), knowledge_list, length_by_kid),
                }
                for p in proxies
            ]
            self._last_decision["connectable_proxy_count"] = len(proxy_pool)
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
        # Consistently use length from the knowledge index as the length-weight source for affinity scoring
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
                # Logging failures must not affect the scheduling path
                pass
        return chosen_kdn, chosen_proxy
