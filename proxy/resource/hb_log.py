# proxy/resource/hb_log.py
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class HBWindow:
    total: int = 0
    ok: int = 0
    fail: int = 0
    last_err: Optional[str] = None


class HeartbeatReporter:
    """
    Proxy 心跳日志聚合器（输出层）：
      - record() 仅做计数，不改变任何业务逻辑
      - report_loop() 每 interval_s 输出一次简报
    线程/协程安全：用 asyncio.Lock 保护窗口。
    """

    def __init__(self, interval_s: float = 30.0):
        self.interval_s = float(interval_s)
        self._lock = asyncio.Lock()
        self._win = HBWindow()
        self._t0 = time.time()

    async def record(self, ok: bool, err: Optional[str] = None) -> None:
        async with self._lock:
            self._win.total += 1
            if ok:
                self._win.ok += 1
            else:
                self._win.fail += 1
                if err:
                    self._win.last_err = err[:200]  # 防止错误信息过长刷屏

    async def snapshot_and_reset(self) -> tuple[float, HBWindow]:
        async with self._lock:
            now = time.time()
            dur = now - self._t0
            w = self._win
            self._win = HBWindow()
            self._t0 = now
            return dur, w


async def hb_report_loop(
    reporter: HeartbeatReporter,
    logger,
    proxy_id: str,
    stop_event: asyncio.Event,
) -> None:
    """
    周期输出简报。注意：这是“输出层”，不影响业务功能。
    """
    while not stop_event.is_set():
        await asyncio.sleep(reporter.interval_s)
        dur, w = await reporter.snapshot_and_reset()

        # 窗口内完全没发生心跳调用：就不输出，避免空刷
        if w.total == 0:
            continue

        lines = []
        lines.append("------ [Proxy HBReport] ------")
        lines.append(f"proxy_id={proxy_id} window_s={dur:.1f}")
        lines.append(f"heartbeat ok/total={w.ok}/{w.total} fail={w.fail}")
        if w.last_err:
            lines.append(f"last_err={w.last_err}")
        lines.append("------------------------------")

        logger.info("\n".join(lines))