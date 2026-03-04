# scheduler/resource/hb_log.py
from __future__ import annotations

"""
HBReport: Heartbeat & Knowledge Refresh brief report (output-layer only)

Design goals:
- DO NOT change scheduler logic; only aggregate noisy logs (heartbeat/refresh) into periodic brief.
- Console shows only ERROR/EXCEPTION; detailed periodic report goes to file logger.
- Thread/async safe enough for FastAPI + asyncio (single event loop). We still use asyncio.Lock.

What we aggregate (per 30s window by default):
1) Proxy heartbeat: total/ok/fail per proxy_id
2) KDN heartbeat: total/ok/fail per kdn_id
3) Knowledge refresh: ok/fail counts + last refresh summary (entries/added/updated/removed/reason)
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, DefaultDict, Optional, Callable, Awaitable, List
from collections import defaultdict

from .proxy_pool import ProxyInfo
from .kdn_pool import KDNInfo

@dataclass
class _HBEntry:
    """单个实体（proxy 或 kdn）在一个窗口内的统计信息。"""
    total: int = 0
    ok: int = 0
    err: int = 0

    # 下面是“最新一次心跳上报”的附加信息（可选）
    last_inflight: Optional[int] = None
    last_qps_1m: Optional[float] = None
    last_gpu_util: Optional[float] = None

    last_items: Optional[int] = None

@dataclass
class _RefreshEntry:
    ok: int = 0
    fail: int = 0
    last_ok: Optional[Dict[str, Any]] = None
    last_fail: Optional[Dict[str, Any]] = None


class HeartbeatLogAggregator:
    """
    仅用于日志聚合：
    - record_proxy / record_kdn：在请求成功/失败时被调用，更新窗口内计数
    - snapshot_and_reset：取出窗口统计并清空，用于定期简报
    """
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._window_start = time.time()
        self._proxy: DefaultDict[str, _HBEntry] = defaultdict(_HBEntry)
        self._kdn: DefaultDict[str, _HBEntry] = defaultdict(_HBEntry)
        self._refresh = _RefreshEntry()

    async def record_proxy(
        self,
        proxy_id: str,
        ok: bool,
        inflight: Optional[int] = None,
        qps_1m: Optional[float] = None,
        gpu_util: Optional[float] = None,
    ) -> None:
        async with self._lock:
            e = self._proxy[proxy_id]
            e.total += 1
            if ok:
                e.ok += 1
            else:
                e.err += 1
            if inflight is not None:
                e.last_inflight = int(inflight)
            if qps_1m is not None:
                e.last_qps_1m = float(qps_1m)
            if gpu_util is not None:
                e.last_gpu_util = float(gpu_util)

    async def record_kdn(
        self,
        kdn_id: str,
        ok: bool,
        items: Optional[int] = None,
        qps_1m: Optional[float] = None,
    ) -> None:
        async with self._lock:
            e = self._kdn[kdn_id]
            e.total += 1
            if ok:
                e.ok += 1
            else:
                e.err += 1
            if items is not None:
                e.last_items = int(items)
            if qps_1m is not None:
                e.last_qps_1m = float(qps_1m)

    async def snapshot_and_reset(self) -> tuple[float, Dict[str, _HBEntry], Dict[str, _HBEntry], _RefreshEntry]:
        async with self._lock:
            now = time.time()
            dur = max(0.0, now - self._window_start)
            proxy = dict(self._proxy)
            kdn = dict(self._kdn)
            refresh = self._refresh

            self._proxy.clear()
            self._kdn.clear()
            self._window_start = now
            self._refresh = _RefreshEntry()
            return dur, proxy, kdn, refresh

    async def record_kdn_refresh(self, r: Dict[str, Any]) -> None:
        """
        记录一次知识刷新结果（来自 kdn_refresh_once 的返回字典）。
        只做统计，绝不影响刷新逻辑。
        """
        ok = bool(r.get("ok"))
        async with self._lock:
            if ok:
                self._refresh.ok += 1
                self._refresh.last_ok = dict(r)
            else:
                self._refresh.fail += 1
                self._refresh.last_fail = dict(r)



async def hb_report_loop(
    agg: HeartbeatLogAggregator,
    logger,
    interval_s: int = 30,
    get_proxies: Optional[Callable[[], Awaitable[List[ProxyInfo]]]] = None,
    get_kdns: Optional[Callable[[], Awaitable[List[KDNInfo]]]] = None,
) -> None:
    """
    周期性输出心跳简报：
    - 每 interval_s 秒取一次 snapshot 并输出
    - 如果窗口内无数据则不输出
    """
    interval_s = int(interval_s)
    while True:
        await asyncio.sleep(interval_s)

        try:
            dur, proxy, kdn, refresh = await agg.snapshot_and_reset()

            # ---- pool snapshot：用于输出“当前负载/存活状态” ----
            proxy_pool_map: Dict[str, ProxyInfo] = {}
            kdn_pool_map: Dict[str, KDNInfo] = {}

            if get_proxies is not None:
                try:
                    proxies = await get_proxies()
                    # proxies: List[ProxyInfo]
                    proxy_pool_map = {p.proxy_id: p for p in proxies}
                except Exception:
                    logger.debug("HBReport get_proxies failed", exc_info=True)

            if get_kdns is not None:
                try:
                    kdns = await get_kdns()
                    # kdns: List[KDNInfo]
                    kdn_pool_map = {k.kdn_id: k for k in kdns}
                except Exception:
                    logger.debug("HBReport get_kdns failed", exc_info=True)
        except Exception:
            logger.exception("heartbeat.report snapshot failed")
            continue

        if (not proxy and not kdn and refresh.ok == 0 and refresh.fail == 0
                and not proxy_pool_map and not kdn_pool_map):
            continue

        lines = []
        lines.append("------")
        lines.append(f"HBReport window={dur:.1f}s interval={interval_s}s")

        lines.append("[Proxy]")
        if proxy:
            for pid, e in sorted(proxy.items(), key=lambda x: x[0]):
                p = proxy_pool_map.get(pid)
                if p is None:
                    lines.append(f"  - proxy_id={pid} ok/total={e.ok}/{e.total} err={e.err} load=n/a")
                else:
                    # 注意：ttl_s 在 ProxyPool 里，用于 is_alive 判定
                    # p.is_alive(pool.ttl_s) 需要 ttl_s，但我们这里只拿到 ProxyInfo，拿不到 pool 对象
                    # 所以这里输出 last_seen_at + “alive未知”。如果你希望 alive，也可以在 get_proxies 返回时一并计算。
                    lines.append(
                        f"  - proxy_id={pid} ok/total={e.ok}/{e.total} err={e.err} "
                        f"load(inflight={int(p.load.inflight)} qps_1m={float(p.load.qps_1m)} gpu_util={float(p.load.gpu_util)}) "
                        f"last_seen_at={float(p.last_seen_at):.3f}"
                    )
        else:
            lines.append("  (no events)")

        lines.append("[KDN]")
        if kdn:
            for kid, e in sorted(kdn.items(), key=lambda x: x[0]):
                k = kdn_pool_map.get(kid)
                if k is None:
                    lines.append(f"  - kdn_id={kid} ok/total={e.ok}/{e.total} err={e.err} load=n/a")
                else:
                    lines.append(
                        f"  - kdn_id={kid} ok/total={e.ok}/{e.total} err={e.err} "
                        f"load(items={int(k.load.items)} qps_1m={float(k.load.qps_1m)}) "
                        f"last_seen_at={float(k.last_seen_at):.3f}"
                    )
        else:
            lines.append("  (no events)")

        lines.append("[Knowledge]")
        lines.append(f"  - refresh_ok={refresh.ok} refresh_fail={refresh.fail}")
        if refresh.last_ok:
            ok = refresh.last_ok
            lines.append(f"  - last_ok: entries={ok.get('entries')} added={ok.get('added')} "
                         f"updated={ok.get('updated')} removed={ok.get('removed')}")
        if refresh.last_fail:
            fail = refresh.last_fail
            reason = fail.get("reason") or fail.get("error") or str(fail)
            lines.append(f"  - last_fail: reason={reason}")

        lines.append("------")

        logger.info("\n" + "\n".join(lines))