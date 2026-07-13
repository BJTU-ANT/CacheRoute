#!/usr/bin/env python3
"""Report local Instance resource-agent snapshots to the Proxy control plane."""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple


def get_json(url: str, timeout_s: float) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def post_json(url: str, payload: Dict[str, Any], timeout_s: float) -> Tuple[int, Dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        text = resp.read().decode("utf-8")
        return resp.status, json.loads(text) if text else {}


def report_once(
    agent_url: str,
    proxy_cp_url: str,
    instance_id: str,
    timeout_s: float,
    log_success: bool = True,
    log_failure: bool = True,
) -> bool:
    snapshot_url = f"{agent_url.rstrip('/')}/v1/resource/snapshot"
    report_url = f"{proxy_cp_url.rstrip('/')}/v1/instance/resource_snapshot"
    snapshot = get_json(snapshot_url, timeout_s=timeout_s)
    status, result = post_json(
        report_url,
        payload={"instance_id": instance_id, "snapshot": snapshot},
        timeout_s=timeout_s,
    )
    ok = bool(result.get("ok"))
    if ok:
        if log_success:
            print(f"[ResourceReporter] report ok instance_id={instance_id} status={status}", flush=True)
        return True
    if log_failure:
        if result.get("error") == "unknown_instance":
            print(f"[ResourceReporter][WARN] unknown_instance: register instance first id={instance_id}", flush=True)
        else:
            print(f"[ResourceReporter][WARN] report rejected status={status} result={result}", flush=True)
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report Instance resource snapshots to Proxy control plane")
    parser.add_argument("--agent-url", default="http://127.0.0.1:9201", help="local Rust resource agent base URL")
    parser.add_argument("--proxy-cp-url", default="http://127.0.0.1:8002", help="Proxy control-plane base URL")
    parser.add_argument("--instance-id", default="hp_127.0.0.1:9001", help="registered Instance id")
    parser.add_argument("--interval-ms", type=int, default=1000, help="report interval in milliseconds")
    parser.add_argument("--timeout-s", type=float, default=2.0, help="HTTP timeout in seconds")
    parser.add_argument("--once", action="store_true", help="send one report and exit")
    parser.add_argument("--quiet-success", action="store_true", help="do not print every successful report")
    parser.add_argument("--failure-log-every", type=int, default=30, help="log repeated failures every N attempts")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    interval_s = max(0.1, float(args.interval_ms) / 1000.0)
    failures = 0
    first_ok = False
    while True:
        try:
            ok = report_once(
                agent_url=args.agent_url,
                proxy_cp_url=args.proxy_cp_url,
                instance_id=args.instance_id,
                timeout_s=args.timeout_s,
                log_success=(not args.quiet_success),
                log_failure=(failures == 0 or (failures + 1) % max(1, args.failure_log_every) == 0),
            )
            if ok:
                failures = 0
                if args.quiet_success and not first_ok:
                    first_ok = True
                    print(f"[ResourceReporter] first report ok instance_id={args.instance_id}", flush=True)
            else:
                failures += 1
        except urllib.error.URLError as exc:
            failures += 1
            if failures == 1 or failures % max(1, args.failure_log_every) == 0:
                print(f"[ResourceReporter][WARN] HTTP unavailable x{failures}: {exc}", flush=True)
        except Exception as exc:
            failures += 1
            if failures == 1 or failures % max(1, args.failure_log_every) == 0:
                print(f"[ResourceReporter][WARN] report failed x{failures}: {exc}", flush=True)
        if args.once:
            return 0
        time.sleep(interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
