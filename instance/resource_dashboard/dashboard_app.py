#!/usr/bin/env python3
"""Tkinter desktop monitor for the CacheRoute Instance Rust resource agent."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError as exc:  # Keep missing tkinter failures actionable in slim containers.
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    TK_IMPORT_ERROR: Optional[ImportError] = exc
else:
    TK_IMPORT_ERROR = None

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_MANIFEST = REPO_ROOT / "instance" / "resource_agent" / "Cargo.toml"
DEFAULT_AGENT_LISTEN = "127.0.0.1:9201"
DEFAULT_SAMPLE_INTERVAL_MS = 1000
DEFAULT_INSTANCE_ID = "hp_127.0.0.1:9001"
POLL_INTERVAL_MS = 1000
DEFAULT_AGENT_START_TIMEOUT_S = 60.0
AGENT_OUTPUT_TAIL_LINES = 20


def parse_listen(value: str) -> Tuple[str, int]:
    raw = (value or "").strip()
    if not raw or ":" not in raw:
        raise argparse.ArgumentTypeError(f"listen address must be host:port, got {value!r}")
    host, port_s = raw.rsplit(":", 1)
    if not host:
        host = "127.0.0.1"
    try:
        port = int(port_s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid port in {value!r}") from exc
    if port <= 0 or port > 65535:
        raise argparse.ArgumentTypeError(f"port out of range in {value!r}")
    return host, port


def request_json(url: str, timeout_s: float = 1.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


class AgentProcess:
    def __init__(self, listen: str, sample_interval_ms: int, instance_id: str, start_timeout_s: float):
        self.listen = listen
        self.sample_interval_ms = int(sample_interval_ms)
        self.instance_id = instance_id
        self.base_url = f"http://{listen}"
        self.start_timeout_s = float(start_timeout_s)
        self.proc: Optional[subprocess.Popen[str]] = None
        self.lock = threading.Lock()
        self.stdout_tail: Deque[str] = deque(maxlen=AGENT_OUTPUT_TAIL_LINES)
        self.stderr_tail: Deque[str] = deque(maxlen=AGENT_OUTPUT_TAIL_LINES)

    def health(self) -> Dict[str, Any]:
        return request_json(f"{self.base_url}/healthz", timeout_s=0.6)

    def snapshot(self) -> Dict[str, Any]:
        return request_json(f"{self.base_url}/v1/resource/snapshot", timeout_s=1.0)

    def is_reachable(self) -> bool:
        try:
            return bool(self.health().get("ok"))
        except Exception:
            return False

    def managed_running(self) -> bool:
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def _capture_stream(self, stream: Any, tail: Deque[str]) -> None:
        try:
            for line in iter(stream.readline, ""):
                with self.lock:
                    tail.append(line.rstrip("\n"))
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def output_tail(self) -> str:
        with self.lock:
            stderr = list(self.stderr_tail)
            stdout = list(self.stdout_tail)
        sections = []
        if stderr:
            sections.append("stderr (last 20 lines):\n" + "\n".join(stderr))
        if stdout:
            sections.append("stdout (last 20 lines):\n" + "\n".join(stdout))
        return "\n\n".join(sections) if sections else "no stdout/stderr captured yet"

    def start(self, status_cb: Optional[Callable[[str], None]] = None) -> str:
        def notify(text: str) -> None:
            if status_cb is not None:
                status_cb(text)

        if self.is_reachable():
            return "agent already reachable"
        notify("starting resource agent...")
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return f"managed agent already running pid={self.proc.pid}"
            self.stdout_tail.clear()
            self.stderr_tail.clear()
            cmd = [
                "cargo", "run", "--manifest-path", str(AGENT_MANIFEST), "--",
                "--listen", self.listen,
                "--sample-interval-ms", str(self.sample_interval_ms),
                "--instance-id", self.instance_id,
            ]
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                bufsize=1,
            )
            proc = self.proc
        if proc.stdout is not None:
            threading.Thread(target=self._capture_stream, args=(proc.stdout, self.stdout_tail), daemon=True).start()
        if proc.stderr is not None:
            threading.Thread(target=self._capture_stream, args=(proc.stderr, self.stderr_tail), daemon=True).start()

        deadline = time.time() + self.start_timeout_s
        notified_waiting = False
        while time.time() < deadline:
            if self.is_reachable():
                with self.lock:
                    pid = self.proc.pid if self.proc is not None else "?"
                return f"started managed agent pid={pid}"
            with self.lock:
                exited = self.proc is not None and self.proc.poll() is not None
                returncode = None if self.proc is None else self.proc.returncode
            if exited:
                return f"resource agent build/run failed: exited with code {returncode}\n\n{self.output_tail()}"
            if not notified_waiting:
                notify("waiting for resource agent... cargo may still be building")
                notified_waiting = True
            time.sleep(0.25)
        return f"resource agent start timed out after {self.start_timeout_s:.1f}s\n\n{self.output_tail()}"

    def stop(self) -> str:
        with self.lock:
            proc = self.proc
            if proc is None:
                return "no dashboard-managed agent"
            if proc.poll() is not None:
                code = proc.returncode
                self.proc = None
                return f"managed agent already exited code={code}"
            pid = proc.pid
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5.0)
        with self.lock:
            if self.proc is proc:
                self.proc = None
        return f"stopped managed agent pid={pid}"


class ResourceDashboardApp:
    def __init__(self, root: "tk.Tk", agent: AgentProcess, auto_start: bool):
        self.root = root
        self.agent = agent
        self.network_history: List[Tuple[float, float]] = []
        self.max_samples = 60
        self._poll_job: Optional[str] = None
        self._snapshot_inflight = False
        self._agent_op_inflight = False
        self._last_agent_error_status: Optional[str] = None
        self._closed = False

        self.root.title("CacheRoute Instance Resource Monitor")
        self.root.geometry("980x720")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.status_var = tk.StringVar(value="initializing")
        self.instance_var = tk.StringVar(value="-")
        self.admission_var = tk.StringVar(value="-")
        self.update_var = tk.StringVar(value="-")
        self.cpu_var = tk.StringVar(value="-")
        self.load_var = tk.StringVar(value="-")
        self.mem_var = tk.StringVar(value="-")
        self.net_var = tk.StringVar(value="-")

        self._build_layout()
        if auto_start:
            self.set_status("starting resource agent...")
            self.run_agent_op(self.agent.start)
        self.schedule_poll(300)

    def _build_layout(self) -> None:
        pad = {"padx": 8, "pady": 6}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="CacheRoute Instance Resource Monitor", font=("TkDefaultFont", 16, "bold")).pack(side="left")
        ttk.Label(top, textvariable=self.status_var).pack(side="right")

        summary = ttk.LabelFrame(self.root, text="Instance Summary")
        summary.pack(fill="x", **pad)
        for label, var in (("Instance ID", self.instance_var), ("Admission", self.admission_var), ("Last update", self.update_var)):
            ttk.Label(summary, text=f"{label}:").pack(side="left", padx=(8, 2), pady=8)
            ttk.Label(summary, textvariable=var, font=("TkDefaultFont", 10, "bold")).pack(side="left", padx=(0, 16), pady=8)

        controls = ttk.LabelFrame(self.root, text="Agent Controls")
        controls.pack(fill="x", **pad)
        ttk.Button(controls, text="Start Agent", command=lambda: self.run_agent_op(self.agent.start)).pack(side="left", padx=6, pady=8)
        ttk.Button(controls, text="Stop Agent", command=lambda: self.run_agent_op(self.agent.stop)).pack(side="left", padx=6, pady=8)
        ttk.Button(controls, text="Refresh Snapshot", command=self.poll_snapshot).pack(side="left", padx=6, pady=8)

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, **pad)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(body)
        right.pack(side="right", fill="both", expand=True)

        cpu = ttk.LabelFrame(left, text="CPU")
        cpu.pack(fill="x", **pad)
        self.cpu_canvas = tk.Canvas(cpu, width=280, height=90, bg="#111827", highlightthickness=0)
        self.cpu_canvas.pack(fill="x", padx=8, pady=4)
        ttk.Label(cpu, textvariable=self.cpu_var).pack(anchor="w", **pad)
        ttk.Label(cpu, textvariable=self.load_var).pack(anchor="w", **pad)

        mem = ttk.LabelFrame(left, text="Memory")
        mem.pack(fill="x", **pad)
        self.mem_canvas = tk.Canvas(mem, width=280, height=90, bg="#111827", highlightthickness=0)
        self.mem_canvas.pack(fill="x", padx=8, pady=4)
        ttk.Label(mem, textvariable=self.mem_var).pack(anchor="w", **pad)

        net = ttk.LabelFrame(left, text="Network")
        net.pack(fill="both", expand=True, **pad)
        ttk.Label(net, textvariable=self.net_var).pack(anchor="w", **pad)
        self.net_canvas = tk.Canvas(net, width=420, height=170, bg="#111827", highlightthickness=0)
        self.net_canvas.pack(fill="both", expand=True, padx=8, pady=4)

        gpu = ttk.LabelFrame(right, text="GPU")
        gpu.pack(fill="both", expand=True, **pad)
        self.gpu_frame = ttk.Frame(gpu)
        self.gpu_frame.pack(fill="both", expand=True, padx=8, pady=8)

        raw = ttk.LabelFrame(right, text="Raw JSON")
        raw.pack(fill="both", expand=True, **pad)
        self.raw_text = tk.Text(raw, height=12, wrap="none")
        self.raw_text.pack(fill="both", expand=True, padx=8, pady=8)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def agent_status_callback(self, text: str) -> None:
        self.root.after(0, lambda: self.set_status(text))

    def run_agent_op(self, op: Any) -> None:
        if self._agent_op_inflight:
            self.set_status("agent operation already running")
            return
        self._agent_op_inflight = True
        self._last_agent_error_status = None
        def worker() -> None:
            try:
                result = op(status_cb=self.agent_status_callback) if op == self.agent.start else op()
            except Exception as exc:
                result = f"agent operation failed: {exc}"
            self.root.after(0, lambda: self.finish_agent_op(result))
        threading.Thread(target=worker, daemon=True).start()

    def finish_agent_op(self, result: str) -> None:
        self._agent_op_inflight = False
        first_line = result.splitlines()[0] if result else "agent operation finished"
        self.set_status(first_line)
        if "resource agent build/run failed" in first_line or "resource agent start timed out" in first_line:
            self._last_agent_error_status = first_line
        if "\n" in result:
            self.raw_text.delete("1.0", "end")
            self.raw_text.insert("1.0", result)
        self.poll_snapshot()

    def poll_snapshot(self) -> None:
        if self._snapshot_inflight:
            return
        self._snapshot_inflight = True
        def worker() -> None:
            try:
                snapshot = self.agent.snapshot()
                err = None
            except Exception as exc:
                snapshot = None
                err = exc
            self.root.after(0, lambda: self.finish_snapshot(snapshot, err))
        threading.Thread(target=worker, daemon=True).start()

    def finish_snapshot(self, snapshot: Optional[Dict[str, Any]], err: Optional[Exception]) -> None:
        self._snapshot_inflight = False
        if self._closed:
            return
        if snapshot is None:
            if self._agent_op_inflight:
                self.set_status("waiting for resource agent... cargo may still be building")
            elif self._last_agent_error_status:
                self.set_status(self._last_agent_error_status)
            else:
                self.set_status(f"agent unavailable: {err}")
        else:
            self._last_agent_error_status = None
            self.render(snapshot)
            managed = "managed" if self.agent.managed_running() else "external/unmanaged"
            self.set_status(f"agent reachable ({managed}) at {self.agent.base_url}")
        self.schedule_poll(POLL_INTERVAL_MS)

    def schedule_poll(self, delay_ms: int) -> None:
        if self._closed:
            return
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
        self._poll_job = self.root.after(delay_ms, self.poll_snapshot)

    def render(self, snapshot: Dict[str, Any]) -> None:
        devices = snapshot.get("devices") or {}
        cpu = devices.get("cpu") or {}
        mem = devices.get("memory") or {}
        gpus = devices.get("gpu") or []
        nets = devices.get("network") or []
        capacity = snapshot.get("capacity_hint") or {}

        ts_ms = snapshot.get("timestamp_ms")
        update = time.strftime("%H:%M:%S", time.localtime(float(ts_ms) / 1000.0)) if ts_ms else "-"
        self.instance_var.set(str(snapshot.get("instance_id") or self.agent.instance_id))
        self.admission_var.set(str(capacity.get("admission_state") or "unknown"))
        self.update_var.set(update)

        cpu_util = float(cpu.get("utilization_pct") or 0.0)
        self.cpu_var.set(f"utilization: {cpu_util:.2f}%")
        self.load_var.set(f"load1/load5/load15: {cpu.get('load1', '-')} / {cpu.get('load5', '-')} / {cpu.get('load15', '-')}")
        self.draw_bar(self.cpu_canvas, cpu_util, "CPU utilization")

        used = float(mem.get("used_mb") or 0.0)
        total = float(mem.get("total_mb") or 0.0)
        free = float(mem.get("free_mb") or 0.0)
        ratio = 100.0 * used / total if total > 0 else 0.0
        free_ratio = float(capacity.get("memory_free_ratio") or 0.0) * 100.0
        self.mem_var.set(f"used/free/total: {used:.0f} / {free:.0f} / {total:.0f} MB    free ratio: {free_ratio:.2f}%")
        self.draw_bar(self.mem_canvas, ratio, "Memory used")

        self.render_gpu(gpus)
        self.render_network(nets)
        raw = json.dumps(snapshot, ensure_ascii=False, indent=2)
        self.raw_text.delete("1.0", "end")
        self.raw_text.insert("1.0", raw)

    def render_gpu(self, gpus: Any) -> None:
        for child in self.gpu_frame.winfo_children():
            child.destroy()
        if not isinstance(gpus, list) or not gpus:
            ttk.Label(self.gpu_frame, text="No GPU detected").pack(anchor="w")
            return
        for gpu in gpus:
            used = float(gpu.get("memory_used_mb") or 0.0)
            total = float(gpu.get("memory_total_mb") or 0.0)
            util = max(0.0, min(100.0, float(gpu.get("utilization_pct") or 0.0)))
            mem_pct = max(0.0, min(100.0, 100.0 * used / total)) if total > 0 else 0.0
            box = ttk.Frame(self.gpu_frame, padding=(0, 4))
            box.pack(fill="x", pady=4)
            title = f"GPU {gpu.get('index', '-')}: {gpu.get('name', '-')}"
            ttk.Label(box, text=title, font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
            ttk.Label(box, text=f"util={util:.2f}%  temp={gpu.get('temperature_c', '-')} C  power={gpu.get('power_w', '-')} W").pack(anchor="w")
            ttk.Progressbar(box, maximum=100.0, value=util).pack(fill="x", pady=(2, 6))
            ttk.Label(box, text=f"memory={used:.0f}/{total:.0f} MB ({mem_pct:.2f}%), free={gpu.get('memory_free_mb', '-')} MB").pack(anchor="w")
            ttk.Progressbar(box, maximum=100.0, value=mem_pct).pack(fill="x", pady=(2, 6))

    def render_network(self, nets: Any) -> None:
        first = nets[0] if isinstance(nets, list) and nets else {}
        rx = float(first.get("rx_mbps") or 0.0)
        tx = float(first.get("tx_mbps") or 0.0)
        self.network_history.append((rx, tx))
        if len(self.network_history) > self.max_samples:
            self.network_history.pop(0)
        self.net_var.set(f"{first.get('iface', '-')}  rx={rx:.3f} Mbps  tx={tx:.3f} Mbps  speed={first.get('speed_mbps', '-')} Mbps")
        self.draw_network()

    def draw_bar(self, canvas: "tk.Canvas", pct_value: float, label: str) -> None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), 280)
        pct_clamped = max(0.0, min(100.0, pct_value))
        canvas.create_rectangle(12, 38, width - 12, 58, fill="#334155", outline="")
        canvas.create_rectangle(12, 38, 12 + (width - 24) * pct_clamped / 100.0, 58, fill="#38bdf8", outline="")
        canvas.create_text(14, 18, text=f"{label}: {pct_clamped:.2f}%", anchor="w", fill="#e5e7eb")

    def draw_network(self) -> None:
        canvas = self.net_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 420)
        height = max(canvas.winfo_height(), 170)
        max_value = max([1.0] + [max(rx, tx) for rx, tx in self.network_history])
        for i in range(5):
            y = 16 + i * (height - 32) / 4
            canvas.create_line(36, y, width - 12, y, fill="#263244")
        def plot(idx: int, color: str) -> None:
            points = []
            for i, pair in enumerate(self.network_history):
                x = 36 + i * (width - 48) / max(1, self.max_samples - 1)
                y = height - 16 - (pair[idx] / max_value) * (height - 32)
                points.extend([x, y])
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=2)
        plot(0, "#38bdf8")
        plot(1, "#22c55e")
        canvas.create_text(40, 10, text=f"max {max_value:.3f} Mbps", anchor="w", fill="#9ca3af")
        canvas.create_text(width - 80, 10, text="rx", fill="#38bdf8")
        canvas.create_text(width - 45, 10, text="tx", fill="#22c55e")

    def on_close(self) -> None:
        self._closed = True
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
        threading.Thread(target=self.agent.stop, daemon=True).start()
        self.root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Tkinter monitor for the CacheRoute resource agent")
    parser.add_argument("--agent-listen", default=DEFAULT_AGENT_LISTEN, help="resource agent address, default 127.0.0.1:9201")
    parser.add_argument("--sample-interval-ms", type=int, default=DEFAULT_SAMPLE_INTERVAL_MS, help="resource agent sample interval")
    parser.add_argument("--instance-id", default=DEFAULT_INSTANCE_ID, help="Instance id passed to the resource agent")
    parser.add_argument("--no-auto-start", action="store_true", help="do not auto-start the Rust resource agent")
    parser.add_argument("--agent-start-timeout-s", type=float, default=DEFAULT_AGENT_START_TIMEOUT_S, help="seconds to wait for auto-started resource agent, default 60")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    parse_listen(args.agent_listen)
    if TK_IMPORT_ERROR is not None:
        print(f"Tkinter is not available in this Python environment: {TK_IMPORT_ERROR}", file=sys.stderr)
        print("Install tkinter support or use the existing browser dashboard fallback.", file=sys.stderr)
        return 2
    agent = AgentProcess(args.agent_listen, args.sample_interval_ms, args.instance_id, args.agent_start_timeout_s)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(
            "Unable to open the Tkinter dashboard window. "
            "A graphical display is required; enable X11 forwarding, WSLg, "
            "or use the existing browser dashboard fallback. "
            f"Tk error: {exc}",
            file=sys.stderr,
        )
        return 2
    ResourceDashboardApp(root, agent, auto_start=not args.no_auto_start)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
