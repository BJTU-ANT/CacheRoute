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
        self._gpu_widgets: Dict[Tuple[int, str], Dict[str, Any]] = {}
        self._gpu_topology: Tuple[Tuple[int, str], ...] = ()
        self._gpu_columns = 2
        self._mousewheel_bound = False

        self.root.title("CacheRoute Instance Resource Monitor")
        self.root.geometry("1400x950")
        self.root.minsize(920, 680)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.status_var = tk.StringVar(value="initializing")
        self.agent_url_var = tk.StringVar(value=self.agent.base_url)
        self.agent_mode_var = tk.StringVar(value="agent: unknown")
        self.instance_var = tk.StringVar(value="-")
        self.admission_var = tk.StringVar(value="-")
        self.update_var = tk.StringVar(value="-")
        self.timestamp_var = tk.StringVar(value="-")
        self.gpu_count_var = tk.StringVar(value="-")
        self.cpu_summary_var = tk.StringVar(value="-")
        self.mem_summary_var = tk.StringVar(value="-")
        self.cpu_var = tk.StringVar(value="-")
        self.load_var = tk.StringVar(value="-")
        self.mem_var = tk.StringVar(value="-")
        self.net_var = tk.StringVar(value="-")
        self.refresh_var = tk.StringVar(value=f"auto-refresh: {POLL_INTERVAL_MS / 1000:.0f}s")
        self.cpu_bar_var = tk.DoubleVar(value=0.0)
        self.mem_bar_var = tk.DoubleVar(value=0.0)

        self._configure_style()
        self._build_layout()
        if auto_start:
            self.set_status("starting resource agent...")
            self.run_agent_op(self.agent.start)
        self.schedule_poll(300)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Header.TFrame", background="#0f172a")
        style.configure("HeaderTitle.TLabel", background="#0f172a", foreground="#e5e7eb", font=("TkDefaultFont", 18, "bold"))
        style.configure("Header.TLabel", background="#0f172a", foreground="#cbd5e1")
        style.configure("Status.TLabel", background="#0f172a", foreground="#7dd3fc", font=("TkDefaultFont", 10, "bold"))
        style.configure("Card.TLabelframe", padding=10)
        style.configure("Card.TLabelframe.Label", font=("TkDefaultFont", 11, "bold"))
        style.configure("MetricTitle.TLabel", font=("TkDefaultFont", 9), foreground="#64748b")
        style.configure("MetricValue.TLabel", font=("TkDefaultFont", 11, "bold"))
        style.configure("GpuTitle.TLabel", font=("TkDefaultFont", 11, "bold"))

    def _build_layout(self) -> None:
        self._build_header()
        self._build_summary()
        self._build_controls()
        self._build_notebook()

    def _build_header(self) -> None:
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(14, 10))
        header.pack(fill="x")
        ttk.Label(header, text="CacheRoute Instance Resource Monitor", style="HeaderTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=1, sticky="e", padx=(12, 0))
        ttk.Label(header, textvariable=self.agent_url_var, style="Header.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.agent_mode_var, style="Header.TLabel").grid(row=1, column=1, sticky="e", pady=(4, 0))
        header.columnconfigure(0, weight=1)

    def _build_summary(self) -> None:
        summary = ttk.LabelFrame(self.root, text="Instance Summary", style="Card.TLabelframe")
        summary.pack(fill="x", padx=10, pady=(10, 6))
        metrics = (
            ("Instance ID", self.instance_var),
            ("Admission", self.admission_var),
            ("Last update", self.update_var),
            ("Snapshot timestamp", self.timestamp_var),
            ("GPU count", self.gpu_count_var),
            ("CPU", self.cpu_summary_var),
            ("Memory free", self.mem_summary_var),
        )
        for idx, (label, var) in enumerate(metrics):
            cell = ttk.Frame(summary, padding=(8, 4))
            cell.grid(row=0, column=idx, sticky="ew")
            ttk.Label(cell, text=label, style="MetricTitle.TLabel").pack(anchor="w")
            ttk.Label(cell, textvariable=var, style="MetricValue.TLabel").pack(anchor="w")
            summary.columnconfigure(idx, weight=1)

    def _build_controls(self) -> None:
        controls = ttk.LabelFrame(self.root, text="Agent Controls", style="Card.TLabelframe")
        controls.pack(fill="x", padx=10, pady=6)
        ttk.Button(controls, text="Start Agent", command=lambda: self.run_agent_op(self.agent.start)).pack(side="left", padx=(0, 8), pady=4)
        ttk.Button(controls, text="Stop Agent", command=lambda: self.run_agent_op(self.agent.stop)).pack(side="left", padx=8, pady=4)
        ttk.Button(controls, text="Refresh Snapshot", command=self.poll_snapshot).pack(side="left", padx=8, pady=4)
        ttk.Label(controls, textvariable=self.refresh_var).pack(side="right", padx=8)

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(6, 10))

        overview = ttk.Frame(self.notebook)
        self.notebook.add(overview, text="Overview")
        self._build_scrollable_overview(overview)

        raw_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(raw_tab, text="Raw JSON / Diagnostics")
        self.raw_text = tk.Text(raw_tab, height=12, wrap="none")
        raw_y = ttk.Scrollbar(raw_tab, orient="vertical", command=self.raw_text.yview)
        raw_x = ttk.Scrollbar(raw_tab, orient="horizontal", command=self.raw_text.xview)
        self.raw_text.configure(yscrollcommand=raw_y.set, xscrollcommand=raw_x.set)
        self.raw_text.grid(row=0, column=0, sticky="nsew")
        raw_y.grid(row=0, column=1, sticky="ns")
        raw_x.grid(row=1, column=0, sticky="ew")
        raw_tab.rowconfigure(0, weight=1)
        raw_tab.columnconfigure(0, weight=1)

    def _build_scrollable_overview(self, parent: "ttk.Frame") -> None:
        # Tkinter scrollable-frame pattern: a Canvas owns one inner Frame and the
        # scrollbar tracks the canvas scrollregion as card sizes change.
        self.scroll_canvas = tk.Canvas(parent, highlightthickness=0, bg="#f8fafc")
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.scroll_canvas.yview)
        self.scroll_frame = ttk.Frame(self.scroll_canvas, padding=8)
        self.scroll_window = self.scroll_canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        self.scroll_canvas.configure(yscrollcommand=scrollbar.set)
        self.scroll_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.scroll_frame.bind("<Configure>", self._on_scroll_frame_configure)
        self.scroll_canvas.bind("<Configure>", self._on_scroll_canvas_configure)
        self.scroll_canvas.bind("<Enter>", self._bind_mousewheel)
        self.scroll_canvas.bind("<Leave>", self._unbind_mousewheel)
        self.scroll_frame.bind("<Enter>", self._bind_mousewheel)
        self.scroll_frame.bind("<Leave>", self._unbind_mousewheel)
        self._build_resource_cards(self.scroll_frame)

    def _bind_mousewheel(self, _event: Optional["tk.Event"] = None) -> None:
        if self._mousewheel_bound:
            return
        self._mousewheel_bound = True
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.root.bind_all("<Button-4>", self._on_mousewheel)
        self.root.bind_all("<Button-5>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event: Optional["tk.Event"] = None) -> None:
        if not self._mousewheel_bound:
            return
        self._mousewheel_bound = False
        self.root.unbind_all("<MouseWheel>")
        self.root.unbind_all("<Button-4>")
        self.root.unbind_all("<Button-5>")

    def _on_mousewheel(self, event: "tk.Event") -> None:
        if getattr(event, "num", None) == 4:
            delta = -3
        elif getattr(event, "num", None) == 5:
            delta = 3
        else:
            delta = -1 * int(event.delta / 120) if event.delta else 0
        if delta:
            self.scroll_canvas.yview_scroll(delta, "units")

    def _on_scroll_frame_configure(self, _event: "tk.Event") -> None:
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def _on_scroll_canvas_configure(self, event: "tk.Event") -> None:
        self.scroll_canvas.itemconfigure(self.scroll_window, width=event.width)
        columns = 2 if event.width >= 1050 else 1
        if columns != self._gpu_columns:
            self._gpu_columns = columns
            self._layout_gpu_cards()

    def _build_resource_cards(self, parent: "ttk.Frame") -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x")
        cpu = ttk.LabelFrame(top, text="CPU", style="Card.TLabelframe")
        cpu.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=6)
        mem = ttk.LabelFrame(top, text="Memory", style="Card.TLabelframe")
        mem.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        net = ttk.LabelFrame(top, text="Network", style="Card.TLabelframe")
        net.grid(row=0, column=2, sticky="nsew", padx=(6, 0), pady=6)
        for col in range(3):
            top.columnconfigure(col, weight=1)

        ttk.Label(cpu, textvariable=self.cpu_var, style="MetricValue.TLabel").pack(anchor="w")
        ttk.Label(cpu, textvariable=self.load_var).pack(anchor="w", pady=(4, 8))
        ttk.Progressbar(cpu, maximum=100.0, variable=self.cpu_bar_var).pack(fill="x")

        ttk.Label(mem, textvariable=self.mem_var, style="MetricValue.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Progressbar(mem, maximum=100.0, variable=self.mem_bar_var).pack(fill="x")

        ttk.Label(net, textvariable=self.net_var, style="MetricValue.TLabel").pack(anchor="w")
        self.net_canvas = tk.Canvas(net, width=420, height=150, bg="#111827", highlightthickness=0)
        self.net_canvas.pack(fill="both", expand=True, pady=(8, 0))

        gpu_section = ttk.LabelFrame(parent, text="GPU Resources", style="Card.TLabelframe")
        gpu_section.pack(fill="x", pady=8)
        self.gpu_frame = ttk.Frame(gpu_section)
        self.gpu_frame.pack(fill="x")

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
            self.write_raw_text(result)
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
                self.agent_mode_var.set("agent: starting")
            elif self._last_agent_error_status:
                self.set_status(self._last_agent_error_status)
                self.agent_mode_var.set("agent: startup failed")
            else:
                self.set_status(f"agent unavailable: {err}")
                self.agent_mode_var.set("agent: unavailable")
        else:
            self._last_agent_error_status = None
            self.render(snapshot)
            managed = "managed" if self.agent.managed_running() else "external/unmanaged"
            self.agent_mode_var.set(f"agent: {managed}")
            self.set_status(f"agent reachable ({managed})")
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
        self.timestamp_var.set(str(ts_ms or "-"))
        self.gpu_count_var.set(str(len(gpus) if isinstance(gpus, list) else 0))

        cpu_util = float(cpu.get("utilization_pct") or 0.0)
        self.cpu_summary_var.set(f"{cpu_util:.1f}%")
        self.cpu_var.set(f"utilization: {cpu_util:.2f}%")
        self.load_var.set(f"load1/load5/load15: {cpu.get('load1', '-')} / {cpu.get('load5', '-')} / {cpu.get('load15', '-')}")
        self.cpu_bar_var.set(max(0.0, min(100.0, cpu_util)))

        used = float(mem.get("used_mb") or 0.0)
        total = float(mem.get("total_mb") or 0.0)
        free = float(mem.get("free_mb") or 0.0)
        ratio = 100.0 * used / total if total > 0 else 0.0
        free_ratio = float(capacity.get("memory_free_ratio") or 0.0) * 100.0
        self.mem_summary_var.set(f"{free_ratio:.1f}%")
        self.mem_var.set(f"used/free/total: {used:.0f} / {free:.0f} / {total:.0f} MB    free ratio: {free_ratio:.2f}%")
        self.mem_bar_var.set(max(0.0, min(100.0, ratio)))

        self.render_gpu(gpus)
        self.render_network(nets)
        self.write_raw_text(json.dumps(snapshot, ensure_ascii=False, indent=2))

    def write_raw_text(self, text: str) -> None:
        yview = self.raw_text.yview()
        self.raw_text.delete("1.0", "end")
        self.raw_text.insert("1.0", text)
        if yview:
            self.raw_text.yview_moveto(yview[0])

    def render_gpu(self, gpus: Any) -> None:
        if not isinstance(gpus, list):
            gpus = []
        topology = tuple((int(gpu.get("index", idx)), str(gpu.get("name", "-"))) for idx, gpu in enumerate(gpus))
        if topology != self._gpu_topology:
            self._rebuild_gpu_cards(gpus, topology)
        for idx, gpu in enumerate(gpus):
            key = (int(gpu.get("index", idx)), str(gpu.get("name", "-")))
            widgets = self._gpu_widgets.get(key)
            if widgets is not None:
                self._update_gpu_card(widgets, gpu)

    def _rebuild_gpu_cards(self, gpus: List[Dict[str, Any]], topology: Tuple[Tuple[int, str], ...]) -> None:
        # Anti-flicker strategy: rebuild GPU cards only when topology changes;
        # regular refreshes update existing labels, bars, and card canvases.
        for child in self.gpu_frame.winfo_children():
            child.destroy()
        self._gpu_widgets.clear()
        self._gpu_topology = topology
        if not gpus:
            ttk.Label(self.gpu_frame, text="No GPU detected").grid(row=0, column=0, sticky="w", padx=8, pady=8)
            return
        for idx, gpu in enumerate(gpus):
            key = (int(gpu.get("index", idx)), str(gpu.get("name", "-")))
            self._gpu_widgets[key] = self._create_gpu_card(self.gpu_frame, gpu)
        self._layout_gpu_cards()

    def _create_gpu_card(self, parent: "ttk.Frame", gpu: Dict[str, Any]) -> Dict[str, Any]:
        frame = ttk.LabelFrame(parent, text=f"GPU {gpu.get('index', '-')}: {gpu.get('name', '-')}", style="Card.TLabelframe")
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        gauge = tk.Canvas(body, width=120, height=120, bg="#ffffff", highlightthickness=0)
        gauge.grid(row=0, column=0, rowspan=4, sticky="nw", padx=(0, 12))
        util_var = tk.StringVar(value="util: -")
        temp_var = tk.StringVar(value="temp: -")
        power_var = tk.StringVar(value="power: -")
        mem_var = tk.StringVar(value="memory: -")
        mem_pct_var = tk.DoubleVar(value=0.0)
        ttk.Label(body, textvariable=util_var, style="MetricValue.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(body, textvariable=temp_var).grid(row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Label(body, textvariable=power_var).grid(row=2, column=1, sticky="w", pady=(4, 0))
        ttk.Label(body, textvariable=mem_var).grid(row=3, column=1, sticky="w", pady=(4, 0))
        ttk.Progressbar(body, maximum=100.0, variable=mem_pct_var).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        body.columnconfigure(1, weight=1)
        return {"frame": frame, "gauge": gauge, "util": util_var, "temp": temp_var, "power": power_var, "mem": mem_var, "mem_pct": mem_pct_var}

    def _layout_gpu_cards(self) -> None:
        if not self._gpu_widgets:
            return
        for child in self.gpu_frame.winfo_children():
            child.grid_forget()
        for idx, widgets in enumerate(self._gpu_widgets.values()):
            row = idx // self._gpu_columns
            col = idx % self._gpu_columns
            widgets["frame"].grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
        for col in range(max(1, self._gpu_columns)):
            self.gpu_frame.columnconfigure(col, weight=1)

    def _update_gpu_card(self, widgets: Dict[str, Any], gpu: Dict[str, Any]) -> None:
        used = float(gpu.get("memory_used_mb") or 0.0)
        total = float(gpu.get("memory_total_mb") or 0.0)
        free = gpu.get("memory_free_mb", "-")
        util = max(0.0, min(100.0, float(gpu.get("utilization_pct") or 0.0)))
        mem_pct = max(0.0, min(100.0, 100.0 * used / total)) if total > 0 else 0.0
        widgets["util"].set(f"utilization: {util:.2f}%")
        widgets["temp"].set(f"temperature: {gpu.get('temperature_c', '-')} C")
        widgets["power"].set(f"power: {gpu.get('power_w', '-')} W")
        widgets["mem"].set(f"memory: {used:.0f}/{total:.0f} MB ({mem_pct:.2f}%), free={free} MB")
        widgets["mem_pct"].set(mem_pct)
        self.draw_gpu_gauge(widgets["gauge"], util)

    def draw_gpu_gauge(self, canvas: "tk.Canvas", pct_value: float) -> None:
        # GPU donut chart: the arc span maps directly to utilization, so many
        # GPU cards can be scanned quickly for hot or idle devices.
        canvas.delete("all")
        pct = max(0.0, min(100.0, pct_value))
        size = 104
        x0 = 8
        y0 = 8
        x1 = x0 + size
        y1 = y0 + size
        color = "#22c55e" if pct < 70 else "#f59e0b" if pct < 90 else "#ef4444"
        canvas.create_oval(x0, y0, x1, y1, outline="#dbeafe", width=12)
        canvas.create_arc(x0, y0, x1, y1, start=90, extent=-pct * 3.6, style="arc", outline=color, width=12)
        canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=f"{pct:.0f}%", font=("TkDefaultFont", 16, "bold"), fill="#0f172a")

    def render_network(self, nets: Any) -> None:
        first = nets[0] if isinstance(nets, list) and nets else {}
        rx = float(first.get("rx_mbps") or 0.0)
        tx = float(first.get("tx_mbps") or 0.0)
        self.network_history.append((rx, tx))
        if len(self.network_history) > self.max_samples:
            self.network_history.pop(0)
        self.net_var.set(f"{first.get('iface', '-')}  rx={rx:.3f} Mbps  tx={tx:.3f} Mbps  speed={first.get('speed_mbps', '-')} Mbps")
        self.draw_network()

    def draw_network(self) -> None:
        canvas = self.net_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 420)
        height = max(canvas.winfo_height(), 150)
        max_value = max([1.0] + [max(rx, tx) for rx, tx in self.network_history])
        for i in range(5):
            y = 16 + i * (height - 36) / 4
            canvas.create_line(42, y, width - 12, y, fill="#263244")
        def plot(idx: int, color: str) -> None:
            points = []
            for i, pair in enumerate(self.network_history):
                x = 42 + i * (width - 56) / max(1, self.max_samples - 1)
                y = height - 20 - (pair[idx] / max_value) * (height - 40)
                points.extend([x, y])
            if len(points) >= 4:
                canvas.create_line(*points, fill=color, width=2)
        plot(0, "#38bdf8")
        plot(1, "#22c55e")
        canvas.create_text(46, 10, text=f"max {max_value:.3f} Mbps", anchor="w", fill="#9ca3af")
        canvas.create_text(width - 120, 10, text="rx", fill="#38bdf8")
        canvas.create_text(width - 80, 10, text="tx", fill="#22c55e")

    def on_close(self) -> None:
        self._closed = True
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
        self._unbind_mousewheel()
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
