#!/usr/bin/env python3
"""Small standard-library dashboard for the CacheRoute Instance resource agent."""
from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
AGENT_MANIFEST = REPO_ROOT / "instance" / "resource_agent" / "Cargo.toml"


def parse_listen(value: str) -> Tuple[str, int]:
    raw = (value or "").strip()
    if not raw or ":" not in raw:
        raise argparse.ArgumentTypeError(f"listen address must be host:port, got {value!r}")
    host, port_s = raw.rsplit(":", 1)
    if not host:
        host = "0.0.0.0"
    try:
        port = int(port_s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid port in {value!r}") from exc
    if port <= 0 or port > 65535:
        raise argparse.ArgumentTypeError(f"port out of range in {value!r}")
    return host, port


def http_get_json(url: str, timeout_s: float = 1.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


class AgentManager:
    def __init__(self, agent_listen: str, sample_interval_ms: int, instance_id: str):
        self.agent_listen = agent_listen
        self.sample_interval_ms = int(sample_interval_ms)
        self.instance_id = instance_id
        self.agent_base_url = f"http://{agent_listen}"
        self._proc: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()

    def health(self) -> Dict[str, Any]:
        return http_get_json(f"{self.agent_base_url}/healthz", timeout_s=0.8)

    def snapshot(self) -> Dict[str, Any]:
        return http_get_json(f"{self.agent_base_url}/v1/resource/snapshot", timeout_s=1.5)

    def is_reachable(self) -> bool:
        try:
            h = self.health()
            return bool(h.get("ok"))
        except Exception:
            return False

    def is_managed_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def status(self) -> Dict[str, Any]:
        reachable = self.is_reachable()
        with self._lock:
            proc = self._proc
            managed = proc is not None
            running = proc is not None and proc.poll() is None
            returncode = None if proc is None else proc.poll()
        return {
            "agent_url": self.agent_base_url,
            "reachable": reachable,
            "managed_by_dashboard": managed,
            "managed_process_running": running,
            "pid": proc.pid if running and proc is not None else None,
            "returncode": returncode,
            "sample_interval_ms": self.sample_interval_ms,
            "instance_id": self.instance_id,
        }

    def start(self) -> Dict[str, Any]:
        if self.is_reachable():
            return {"ok": True, "started": False, "reason": "agent_already_reachable", "status": self.status()}
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                already_running = True
            else:
                already_running = False
        if already_running:
            return {"ok": True, "started": False, "reason": "managed_agent_already_running", "status": self.status()}
        with self._lock:
            cmd = [
                "cargo", "run", "--manifest-path", str(AGENT_MANIFEST), "--",
                "--listen", self.agent_listen,
                "--sample-interval-ms", str(self.sample_interval_ms),
                "--instance-id", self.instance_id,
            ]
            print(f"[ResourceDashboard] starting agent: {' '.join(cmd)}", flush=True)
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                text=True,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
        deadline = time.time() + 12.0
        last_err = ""
        while time.time() < deadline:
            if self.is_reachable():
                return {"ok": True, "started": True, "status": self.status()}
            with self._lock:
                if self._proc is not None and self._proc.poll() is not None:
                    return {"ok": False, "started": False, "error": "agent_exited", "status": self.status()}
            last_err = "agent_not_reachable_yet"
            time.sleep(0.25)
        return {"ok": False, "started": False, "error": last_err, "status": self.status()}

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            proc = self._proc
            if proc is None:
                proc_missing = True
                proc_exited = False
            elif proc.poll() is not None:
                self._proc = None
                proc_missing = False
                proc_exited = True
            else:
                proc_missing = False
                proc_exited = False
                print(f"[ResourceDashboard] stopping managed agent pid={proc.pid}", flush=True)
                proc.terminate()
        if proc_missing:
            return {"ok": True, "stopped": False, "reason": "no_managed_agent", "status": self.status()}
        if proc_exited:
            return {"ok": True, "stopped": False, "reason": "managed_agent_already_exited", "status": self.status()}
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
        with self._lock:
            self._proc = None
        return {"ok": True, "stopped": True, "status": self.status()}


class DashboardHandler(SimpleHTTPRequestHandler):
    manager: AgentManager

    def translate_path(self, path: str) -> str:
        # Serve the static frontend from instance/resource_dashboard/static.
        if path == "/" or path.startswith("/static/"):
            rel = "index.html" if path == "/" else path[len("/static/"):]
        else:
            rel = path.lstrip("/")
        return str((STATIC_DIR / rel).resolve())

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[ResourceDashboard] {self.address_string()} - {fmt % args}", flush=True)

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", "0") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/api/health":
            self._send_json(200, {"ok": True, "dashboard": "ok", "agent": self.manager.status()})
            return
        if self.path == "/api/agent/status":
            self._send_json(200, {"ok": True, "status": self.manager.status()})
            return
        if self.path == "/api/snapshot":
            try:
                self._send_json(200, {"ok": True, "snapshot": self.manager.snapshot(), "agent": self.manager.status()})
            except Exception as exc:
                self._send_json(503, {"ok": False, "error": "agent_unavailable", "detail": str(exc), "agent": self.manager.status()})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._read_body()
        if self.path == "/api/agent/start":
            result = self.manager.start()
            self._send_json(200 if result.get("ok") else 500, result)
            return
        if self.path == "/api/agent/stop":
            result = self.manager.stop()
            self._send_json(200 if result.get("ok") else 500, result)
            return
        self._send_json(404, {"ok": False, "error": "not_found"})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CacheRoute Instance resource dashboard")
    p.add_argument("--dashboard-listen", default="0.0.0.0:9102", help="dashboard listen address")
    p.add_argument("--agent-listen", default="127.0.0.1:9101", help="resource agent listen address")
    p.add_argument("--sample-interval-ms", type=int, default=1000, help="agent sample interval")
    p.add_argument("--instance-id", default="hp_127.0.0.1:9001", help="Instance id to pass to the agent")
    p.add_argument("--no-auto-start", action="store_true", help="do not start the resource agent on dashboard startup")
    return p


def main() -> int:
    args = build_parser().parse_args()
    dashboard_host, dashboard_port = parse_listen(args.dashboard_listen)
    parse_listen(args.agent_listen)

    manager = AgentManager(
        agent_listen=args.agent_listen,
        sample_interval_ms=args.sample_interval_ms,
        instance_id=args.instance_id,
    )
    DashboardHandler.manager = manager

    if not args.no_auto_start:
        result = manager.start()
        print(f"[ResourceDashboard] auto-start result: {json.dumps(result, ensure_ascii=False)}", flush=True)
    else:
        print("[ResourceDashboard] auto-start disabled", flush=True)

    server = ThreadingHTTPServer((dashboard_host, dashboard_port), DashboardHandler)
    print(f"[ResourceDashboard] serving http://{dashboard_host}:{dashboard_port}", flush=True)
    print(f"[ResourceDashboard] repo root: {REPO_ROOT}", flush=True)

    def _shutdown(signum: int, _frame: Any) -> None:
        print(f"[ResourceDashboard] signal={signum}, shutting down", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        manager.stop()
        print("[ResourceDashboard] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
