#!/usr/bin/env python3
"""Lightweight manual/e2e validation for demo resource monitoring.

This script starts demo_proxy.py and demo_instance.py, waits until Proxy observes
resource metadata from the Instance, then terminates the Instance and verifies
that the demo-owned Resource Agent no longer answers on the configured port.

It is intentionally a practical smoke script rather than a pytest test because
local environments may not have uvicorn/cargo available.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

ROOT_DIR = Path(__file__).resolve().parents[1]


def get_json(url: str, timeout_s: float = 2.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_for(predicate, timeout_s: float, interval_s: float = 0.5) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval_s)
    return False


def stop_process(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Run CacheRoute demo resource-monitoring smoke validation")
    parser.add_argument("--proxy-host", default="127.0.0.1")
    parser.add_argument("--proxy-port", type=int, default=8001)
    parser.add_argument("--proxy-cp-url", default="http://127.0.0.1:8002")
    parser.add_argument("--instance-host", default="127.0.0.1")
    parser.add_argument("--instance-port", type=int, default=9001)
    parser.add_argument("--agent-listen", default="127.0.0.1:9201")
    parser.add_argument("--agent-url", default="http://127.0.0.1:9201")
    parser.add_argument("--timeout-s", type=float, default=45.0)
    args = parser.parse_args()

    if shutil.which("cargo") is None:
        print("[demo-e2e][SKIP] cargo is not available; cannot auto-start Rust Resource Agent", file=sys.stderr)
        return 2

    proxy = None
    instance = None
    try:
        proxy = subprocess.Popen(
            [
                sys.executable,
                str(ROOT_DIR / "test" / "demo_proxy.py"),
                "--host",
                args.proxy_host,
                "--port",
                str(args.proxy_port),
                "--strategy",
                "round_robin",
                "--injection-strategy",
                "iws",
            ],
            cwd=str(ROOT_DIR),
            start_new_session=True,
        )
        if not wait_for(lambda: bool(get_json(f"{args.proxy_cp_url}/healthz")), args.timeout_s):
            raise RuntimeError("Proxy control plane did not become healthy")

        instance = subprocess.Popen(
            [
                sys.executable,
                str(ROOT_DIR / "test" / "demo_instance.py"),
                "--host",
                args.instance_host,
                "--port",
                str(args.instance_port),
                "--proxy-cp-url",
                args.proxy_cp_url,
                "--resource-monitor",
                "--resource-agent-listen",
                args.agent_listen,
                "--resource-agent-url",
                args.agent_url,
                "--resource-report-interval-ms",
                "500",
            ],
            cwd=str(ROOT_DIR),
            start_new_session=True,
        )

        def proxy_has_resource() -> bool:
            try:
                data = get_json(f"{args.proxy_cp_url}/debug/instance_resources")
            except Exception:
                return False
            for item in data.get("instances", []):
                resource = item.get("resource") or {}
                if resource.get("resource_ts_ms") and resource.get("resource_report_wall_time_ms"):
                    return True
            return False

        if not wait_for(proxy_has_resource, args.timeout_s):
            raise RuntimeError("Proxy did not observe resource reports before timeout")

        stop_process(instance)
        instance = None

        def agent_gone() -> bool:
            try:
                get_json(f"{args.agent_url}/healthz", timeout_s=0.5)
                return False
            except Exception:
                return True

        if not wait_for(agent_gone, 10.0):
            raise RuntimeError("Resource Agent still responds after demo_instance shutdown")

        print("[demo-e2e] ok: Proxy observed resource reports and Resource Agent was cleaned up")
        return 0
    except urllib.error.URLError as exc:
        print(f"[demo-e2e][FAIL] HTTP error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[demo-e2e][FAIL] {exc}", file=sys.stderr)
        return 1
    finally:
        stop_process(instance)
        stop_process(proxy)


if __name__ == "__main__":
    raise SystemExit(main())
