import uvicorn
import argparse
import os
import sys
import subprocess
import atexit
import signal
import shutil

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from core import config


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _start_sidecar(cmd, name: str, env=None):
    try:
        proc = subprocess.Popen(cmd, cwd=str(ROOT_DIR), env=env or os.environ.copy())
    except FileNotFoundError:
        print(f"[demo_instance][WARN] {name} executable not found; skip sidecar: {cmd[0]}", flush=True)
        return None
    except Exception as exc:
        print(f"[demo_instance][WARN] failed to start {name}: {exc}", flush=True)
        return None
    print(f"[demo_instance] started {name} pid={proc.pid}: {' '.join(map(str, cmd))}", flush=True)
    return proc


def _terminate_sidecars(procs):
    for name, proc in procs:
        if proc is None or proc.poll() is not None:
            continue
        try:
            print(f"[demo_instance] stopping {name} pid={proc.pid}", flush=True)
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass


def _install_sidecar_cleanup(procs):
    atexit.register(_terminate_sidecars, procs)

    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)

    def _handler(signum, frame):
        _terminate_sidecars(procs)
        previous = prev_int if signum == signal.SIGINT else prev_term
        if callable(previous):
            previous(signum, frame)
        else:
            raise SystemExit(128 + int(signum))

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main():
    # ====== 启动 Instance 服务 ======
    parser = argparse.ArgumentParser(description="Run CacheRoute Instance")
    parser.add_argument("--host", type=str, default=None, help="listen host (override config)")
    parser.add_argument("--port", type=int, default=None, help="listen port (override config)")
    parser.add_argument(
        "--kdn-targets",
        type=str,
        default=None,
        help="optional topology discovery targets, comma separated (e.g. 127.0.0.1:9101,127.0.0.1:9102)",
    )
    parser.add_argument(
        "--resource-agent",
        dest="resource_agent",
        action="store_true",
        default=_env_flag("INSTANCE_RESOURCE_AGENT_ENABLE", True),
        help="start the local Rust resource agent sidecar (default: enabled; env INSTANCE_RESOURCE_AGENT_ENABLE=0 disables)",
    )
    parser.add_argument(
        "--no-resource-agent",
        dest="resource_agent",
        action="store_false",
        help="do not start the local Rust resource agent sidecar",
    )
    parser.add_argument(
        "--resource-agent-host",
        type=str,
        default=os.environ.get("INSTANCE_RESOURCE_AGENT_HOST", "127.0.0.1"),
        help="resource agent listen host",
    )
    parser.add_argument(
        "--resource-agent-port",
        type=int,
        default=int(os.environ.get("INSTANCE_RESOURCE_AGENT_PORT", getattr(config, "INSTANCE_RESOURCE_AGENT_PORT", 9201))),
        help="resource agent listen port",
    )
    parser.add_argument(
        "--resource-agent-interval-ms",
        type=int,
        default=int(os.environ.get("INSTANCE_RESOURCE_AGENT_INTERVAL_MS", getattr(config, "INSTANCE_RESOURCE_AGENT_INTERVAL_MS", 1000))),
        help="resource agent sampling interval in milliseconds",
    )
    parser.add_argument(
        "--resource-report",
        dest="resource_report",
        action="store_true",
        default=_env_flag("INSTANCE_RESOURCE_REPORT_ENABLE", True),
        help="start Python reporter that sends resource snapshots to Proxy CP (default: enabled)",
    )
    parser.add_argument(
        "--no-resource-report",
        dest="resource_report",
        action="store_false",
        help="do not start the resource snapshot reporter",
    )
    parser.add_argument("--proxy-cp-url", type=str, default=os.environ.get("PROXY_CP_URL", config.PROXY_CP_URL), help="Proxy control-plane URL for registration/reporting")
    args = parser.parse_args()

    # 默认来自 config / env；若命令行提供则覆盖
    cfg_port = int(os.environ.get("INSTANCE_PORT", config.INSTANCE_PORT))
    cfg_host = os.environ.get("INSTANCE_HOST", config.INSTANCE_HOST)

    host = args.host if args.host is not None else cfg_host
    port = args.port if args.port is not None else cfg_port

    # 保证 “监听端口 == 注册上报端口”
    # instance_api.py 的 lifespan 读取 INSTANCE_ADVERTISE_HOST/PORT 与 INSTANCE_PORT
    os.environ["INSTANCE_ADVERTISE_HOST"] = host
    os.environ["INSTANCE_ADVERTISE_PORT"] = str(port)
    os.environ["INSTANCE_PORT"] = str(port)
    os.environ["PROXY_CP_URL"] = args.proxy_cp_url.rstrip("/")
    if args.kdn_targets:
        os.environ["INSTANCE_TOPOLOGY_KDN_TARGETS"] = args.kdn_targets.strip()

    # （可选但强烈建议）确保每个实例 id 唯一，避免 pool upsert 覆盖
    os.environ.setdefault("INSTANCE_ID", f"hp_{host}:{port}")
    instance_id = os.environ["INSTANCE_ID"]

    sidecars = []
    agent_url = f"http://{args.resource_agent_host}:{args.resource_agent_port}"
    if args.resource_agent:
        cargo = shutil.which("cargo")
        if cargo is None:
            print("[demo_instance][WARN] cargo not found; resource agent sidecar is disabled", flush=True)
        else:
            sidecars.append((
                "resource_agent",
                _start_sidecar(
                    [
                        cargo,
                        "run",
                        "--quiet",
                        "--manifest-path",
                        str(ROOT_DIR / "instance" / "resource_agent" / "Cargo.toml"),
                        "--",
                        "--listen",
                        f"{args.resource_agent_host}:{args.resource_agent_port}",
                        "--sample-interval-ms",
                        str(args.resource_agent_interval_ms),
                        "--instance-id",
                        instance_id,
                    ],
                    "resource_agent",
                ),
            ))
    if args.resource_report:
        sidecars.append((
            "resource_reporter",
            _start_sidecar(
                [
                    sys.executable,
                    str(ROOT_DIR / "instance" / "resource_agent" / "proxy_reporter.py"),
                    "--agent-url",
                    agent_url,
                    "--proxy-cp-url",
                    args.proxy_cp_url.rstrip("/"),
                    "--instance-id",
                    instance_id,
                    "--interval-ms",
                    str(args.resource_agent_interval_ms),
                ],
                "resource_reporter",
            ),
        ))
    _install_sidecar_cleanup(sidecars)

    from instance import instance
    uvicorn.run(instance, host=host, port=port, reload=False)

if __name__ == "__main__":
    main()
