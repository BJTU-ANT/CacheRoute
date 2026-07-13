import uvicorn
import argparse
import os
import sys

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


def _set_bool_env(name: str, enabled: bool) -> None:
    os.environ[name] = "1" if enabled else "0"


def _interval_from_hz(hz: float) -> int:
    return max(1, int(round(1000.0 / max(float(hz), 0.001))))


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
        "--resource-monitor",
        action="store_true",
        default=_env_flag("INSTANCE_RESOURCE_MONITOR_ENABLE", config.INSTANCE_RESOURCE_MONITOR_ENABLE),
        help="enable Resource Agent startup and Proxy resource reporting (default: disabled)",
    )
    parser.add_argument(
        "--resource-agent",
        dest="resource_agent",
        action="store_true",
        default=None,
        help="auto-start or reuse the local Rust resource agent when resource monitoring is enabled",
    )
    parser.add_argument(
        "--no-resource-agent",
        dest="resource_agent",
        action="store_false",
        help="do not auto-start the Rust resource agent; only report if an external agent is reachable",
    )
    parser.add_argument(
        "--resource-report",
        dest="resource_report",
        action="store_true",
        default=None,
        help="send resource snapshots to Proxy after Instance registration when monitoring is enabled",
    )
    parser.add_argument(
        "--no-resource-report",
        dest="resource_report",
        action="store_false",
        help="do not send resource snapshots to Proxy",
    )
    parser.add_argument(
        "--resource-agent-listen",
        type=str,
        default=os.environ.get("INSTANCE_RESOURCE_AGENT_LISTEN", config.INSTANCE_RESOURCE_AGENT_LISTEN),
        help="resource agent listen address (host:port)",
    )
    parser.add_argument(
        "--resource-agent-url",
        type=str,
        default=os.environ.get("INSTANCE_RESOURCE_AGENT_URL", config.INSTANCE_RESOURCE_AGENT_URL),
        help="resource agent base URL used by the reporter",
    )
    parser.add_argument(
        "--resource-agent-sample-interval-ms",
        type=int,
        default=int(os.environ.get("INSTANCE_RESOURCE_AGENT_SAMPLE_INTERVAL_MS", config.INSTANCE_RESOURCE_AGENT_SAMPLE_INTERVAL_MS)),
        help="resource agent sampling interval in milliseconds",
    )
    parser.add_argument(
        "--resource-agent-start-timeout-s",
        type=float,
        default=float(os.environ.get("INSTANCE_RESOURCE_AGENT_START_TIMEOUT_S", config.INSTANCE_RESOURCE_AGENT_START_TIMEOUT_S)),
        help="seconds to wait for Resource Agent health before reporting",
    )
    parser.add_argument(
        "--resource-report-hz",
        type=float,
        default=float(os.environ.get("INSTANCE_RESOURCE_REPORT_HZ", config.INSTANCE_RESOURCE_REPORT_HZ)),
        help="resource reporting frequency in Hz; ignored when --resource-report-interval-ms is set",
    )
    parser.add_argument(
        "--resource-report-interval-ms",
        type=int,
        default=None,
        help="resource report interval in milliseconds; overrides --resource-report-hz",
    )
    parser.add_argument(
        "--resource-report-timeout-s",
        type=float,
        default=float(os.environ.get("INSTANCE_RESOURCE_REPORT_TIMEOUT_S", config.INSTANCE_RESOURCE_REPORT_TIMEOUT_S)),
        help="HTTP timeout for resource reporting",
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

    monitor_enabled = bool(args.resource_monitor)
    auto_start_agent = config.INSTANCE_RESOURCE_AUTO_START_AGENT if args.resource_agent is None else bool(args.resource_agent)
    report_enabled = config.INSTANCE_RESOURCE_REPORT_ENABLE if args.resource_report is None else bool(args.resource_report)
    if monitor_enabled and args.resource_report is None:
        report_enabled = True

    report_interval_ms = args.resource_report_interval_ms
    if report_interval_ms is None:
        report_interval_ms = _interval_from_hz(args.resource_report_hz)

    _set_bool_env("INSTANCE_RESOURCE_MONITOR_ENABLE", monitor_enabled)
    _set_bool_env("INSTANCE_RESOURCE_AUTO_START_AGENT", auto_start_agent)
    _set_bool_env("INSTANCE_RESOURCE_REPORT_ENABLE", report_enabled)
    os.environ["INSTANCE_RESOURCE_AGENT_LISTEN"] = args.resource_agent_listen
    os.environ["INSTANCE_RESOURCE_AGENT_URL"] = args.resource_agent_url.rstrip("/")
    os.environ["INSTANCE_RESOURCE_AGENT_SAMPLE_INTERVAL_MS"] = str(args.resource_agent_sample_interval_ms)
    os.environ["INSTANCE_RESOURCE_AGENT_START_TIMEOUT_S"] = str(args.resource_agent_start_timeout_s)
    os.environ["INSTANCE_RESOURCE_REPORT_HZ"] = str(args.resource_report_hz)
    os.environ["INSTANCE_RESOURCE_REPORT_INTERVAL_MS"] = str(report_interval_ms)
    os.environ["INSTANCE_RESOURCE_REPORT_TIMEOUT_S"] = str(args.resource_report_timeout_s)

    if monitor_enabled:
        print(
            "[demo_instance] resource monitor enabled: "
            f"agent_listen={args.resource_agent_listen} agent_url={args.resource_agent_url.rstrip('/')} "
            f"auto_start_agent={auto_start_agent} report_enabled={report_enabled} "
            f"report_interval_ms={report_interval_ms}",
            flush=True,
        )
    else:
        print("[demo_instance] resource monitor disabled (default)", flush=True)

    from instance import instance
    uvicorn.run(instance, host=host, port=port, reload=False)

if __name__ == "__main__":
    main()
