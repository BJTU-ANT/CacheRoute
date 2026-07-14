"""
demo_proxy.py

启动 Proxy 服务，用于接收 Scheduler 转发的 Request payload。
"""

import sys
import argparse
import os
import subprocess
import atexit
import signal
import time
import urllib.error
import urllib.request
import importlib.util

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

_CONFIG_SPEC = importlib.util.spec_from_file_location("cacheroute_core_config", ROOT_DIR / "core" / "config.py")
if _CONFIG_SPEC is None or _CONFIG_SPEC.loader is None:
    raise RuntimeError("failed to load core/config.py")
config = importlib.util.module_from_spec(_CONFIG_SPEC)
_CONFIG_SPEC.loader.exec_module(config)


def _tail_text(data: bytes, max_chars: int = 1200) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    if len(text) <= max_chars:
        return text
    return "..." + text[-max_chars:]


def _terminate_process_group(proc: subprocess.Popen, timeout_s: float = 3.0) -> None:
    """Best-effort cleanup for the UI process started by this demo only."""
    if proc.poll() is not None:
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return

    try:
        proc.wait(timeout=timeout_s)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            return
    try:
        proc.wait(timeout=1.0)
    except Exception:
        pass


def _read_ui_output_tail(proc: subprocess.Popen) -> str:
    output = b""
    if proc.stdout is not None:
        try:
            output = proc.stdout.read() or b""
        except Exception:
            output = b""
    return _tail_text(output)


def _wait_for_proxy_ui_ready(proc: subprocess.Popen, ui_url: str, timeout_s: float = 4.0) -> tuple[bool, str]:
    """Wait briefly for the UI subprocess and verify its /api/config endpoint."""
    deadline = time.time() + timeout_s
    config_url = f"{ui_url.rstrip('/')}/api/config"
    last_error = "not checked yet"

    while time.time() < deadline:
        exit_code = proc.poll()
        if exit_code is not None:
            output_tail = _read_ui_output_tail(proc)
            detail = f"process exited early with code {exit_code}"
            if output_tail:
                detail += f"; output tail: {output_tail}"
            return False, detail

        try:
            with urllib.request.urlopen(config_url, timeout=0.5) as resp:
                if 200 <= int(resp.status) < 300:
                    return True, "ready"
                last_error = f"GET {config_url} returned HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            last_error = f"GET {config_url} returned HTTP {exc.code}"
        except Exception as exc:
            last_error = f"GET {config_url} failed: {exc}"

        time.sleep(0.2)

    if proc.poll() is not None:
        output_tail = _read_ui_output_tail(proc)
        detail = f"process exited with code {proc.returncode}"
        if output_tail:
            detail += f"; output tail: {output_tail}"
        return False, detail

    return False, f"readiness timeout after {timeout_s:.1f}s; last_error={last_error}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run CacheRoute Proxy")
    parser.add_argument("--host", type=str, default=None, help="proxy listen host (default from config/env)")
    parser.add_argument("--port", type=int, default=None, help="proxy listen port (default from config/env)")
    parser.add_argument(
        "--strategy",
        type=str,
        default=None,
        help="instance scheduling strategy (e.g., round_robin, least_inflight)",
    )
    parser.add_argument(
        "--kdn-links-json",
        type=str,
        default=config.PROXY_KDN_LINKS_JSON,
        help="optional JSON string for PROXY_KDN_LINKS_JSON (CacheRoute topology tiers)",
    )
    parser.add_argument(
        "--injection-strategy",
        type=str,
        default=None,
        help="proxy injection strategy (default|iws)",
    )
    parser.add_argument(
        "--ready-release-policy",
        type=str,
        default=None,
        choices=("ordered", "text_bypass"),
        help="ready release policy (ordered|text_bypass)",
    )
    parser.add_argument(
        "--proxy-ui",
        dest="proxy_ui",
        action="store_true",
        default=True,
        help="start lightweight browser Proxy UI (default: enabled)",
    )
    parser.add_argument(
        "--no-proxy-ui",
        dest="proxy_ui",
        action="store_false",
        help="disable the browser Proxy UI subprocess",
    )
    parser.add_argument(
        "--proxy-ui-listen",
        type=str,
        default=os.environ.get("PROXY_UI_LISTEN", "127.0.0.1:8202"),
        help="Proxy UI listen address as host:port (default: 127.0.0.1:8202)",
    )
    parser.add_argument(
        "--proxy-ui-url",
        type=str,
        default=os.environ.get("PROXY_UI_URL", ""),
        help="browser-facing Proxy UI URL to print; defaults to http://<--proxy-ui-listen>",
    )
    args = parser.parse_args()

    if args.strategy:
        os.environ["PROXY_INSTANCE_STRATEGY"] = args.strategy
    if args.injection_strategy:
        normalized = args.injection_strategy.strip().lower()
        if normalized not in {"default", "iws"}:
            parser.error("--injection-strategy must be one of: default, iws")
        os.environ["PROXY_INJECTION_STRATEGY"] = normalized
    if args.kdn_links_json and str(args.kdn_links_json).strip():
        os.environ["PROXY_KDN_LINKS_JSON"] = args.kdn_links_json
    if args.ready_release_policy:
        os.environ["PROXY_READY_RELEASE_POLICY"] = args.ready_release_policy

    cfg_host = os.environ.get("PROXY_DP_HOST", config.PROXY_DP_HOST)
    cfg_port = int(os.environ.get("PROXY_DP_PORT", config.PROXY_DP_PORT))
    host = args.host if args.host is not None else cfg_host
    port = args.port if args.port is not None else cfg_port

    # Keep data-plane bind address and scheduler-advertised address aligned for demos.
    os.environ["PROXY_ADVERTISE_HOST"] = host
    os.environ["PROXY_ADVERTISE_PORT"] = str(port)
    os.environ["PROXY_DP_HOST"] = host
    os.environ["PROXY_DP_PORT"] = str(port)

    ui_proc = None
    if args.proxy_ui:
        try:
            ui_host, ui_port_raw = args.proxy_ui_listen.rsplit(":", 1)
            ui_port = int(ui_port_raw)
        except ValueError:
            parser.error("--proxy-ui-listen must use host:port format")

        cp_host = os.environ.get("PROXY_CP_HOST", config.PROXY_CP_HOST)
        cp_port = int(os.environ.get("PROXY_CP_PORT", config.PROXY_CP_PORT))
        ui_env = os.environ.copy()
        ui_env.setdefault("PROXY_UI_PROXY_CP_URL", f"http://{cp_host}:{cp_port}")
        ui_env.setdefault("PROXY_UI_SCHEDULER_CP_URL", os.environ.get("SCHEDULER_CP_URL", config.SCHEDULER_CP_URL))
        ui_env.setdefault("PROXY_UI_PROXY_ID", os.environ.get("PROXY_ID", f"hp_{host}:{port}"))
        ui_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "UI.proxy_ui.proxy_ui_server:app",
                "--host",
                ui_host,
                "--port",
                str(ui_port),
                "--log-level",
                "warning",
            ],
            cwd=str(ROOT_DIR),
            env=ui_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        atexit.register(_terminate_process_group, ui_proc)
        ui_url = args.proxy_ui_url.strip() or f"http://{ui_host}:{ui_port}"
        ui_ready, ui_detail = _wait_for_proxy_ui_ready(ui_proc, ui_url)
        if ui_ready:
            print(f"[demo_proxy] Proxy UI available at: {ui_url}", flush=True)
        else:
            print(f"[demo_proxy][WARN] Proxy UI failed to start: {ui_detail}", flush=True)
            _terminate_process_group(ui_proc)
            ui_proc = None

    import uvicorn
    from proxy import proxy  # 确保在设置环境变量后导入

    try:
        # 选择一个与 Scheduler 不同的端口，例如 8001
        uvicorn.run(proxy, host=host, port=port, reload=False)
    finally:
        if ui_proc is not None:
            _terminate_process_group(ui_proc)
