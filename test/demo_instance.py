import uvicorn
import argparse
import os
import sys
import asyncio
import signal
import subprocess
import shutil
import time
import urllib.request
from collections import deque
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from core import config
from instance.resource_agent.proxy_reporter import report_once


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _set_bool_env(name: str, enabled: bool) -> None:
    os.environ[name] = "1" if enabled else "0"


def _interval_from_hz(hz: float) -> int:
    return max(1, int(round(1000.0 / max(float(hz), 0.001))))


class DemoResourceMonitor:
    """Own Resource Agent lifecycle and reporting for demo_instance.py only."""

    def __init__(
        self,
        *,
        enabled: bool,
        auto_start_agent: bool,
        report_enabled: bool,
        agent_listen: str,
        agent_url: str,
        sample_interval_ms: int,
        start_timeout_s: float,
        report_interval_ms: int,
        report_timeout_s: float,
        proxy_cp_url: str,
    ) -> None:
        self.enabled = enabled
        self.auto_start_agent = auto_start_agent
        self.report_enabled = report_enabled
        self.agent_listen = agent_listen
        self.agent_url = agent_url.rstrip("/")
        self.sample_interval_ms = int(sample_interval_ms)
        self.start_timeout_s = float(start_timeout_s)
        self.report_interval_ms = int(report_interval_ms)
        self.report_timeout_s = float(report_timeout_s)
        self.proxy_cp_url = proxy_cp_url.rstrip("/")
        self._proc = None
        self._report_task = None
        self._started_agent = False

    @staticmethod
    def _read_tail(lines: deque[str]) -> str:
        return "".join(lines).strip()

    @staticmethod
    def _spawn_log_reader(stream, tail: deque[str]) -> None:
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                if not raw:
                    break
                tail.append(raw.decode("utf-8", errors="replace"))
        except Exception:
            return

    def _agent_health_ok(self, timeout_s: float = 1.0) -> bool:
        try:
            req = urllib.request.Request(f"{self.agent_url}/healthz", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return 200 <= int(resp.status) < 300
        except Exception:
            return False

    async def _wait_agent_ready(self, logger) -> bool:
        deadline = time.monotonic() + max(0.1, self.start_timeout_s)
        while time.monotonic() < deadline:
            if await asyncio.to_thread(self._agent_health_ok, 1.0):
                logger.info("[demo_instance][resource] agent ready: %s", self.agent_url)
                return True
            await asyncio.sleep(0.2)
        return False

    async def _start_or_reuse_agent(self, runtime_instance_id: str, logger) -> bool:
        if await asyncio.to_thread(self._agent_health_ok, 1.0):
            logger.info("[demo_instance][resource] reusing reachable Resource Agent: %s", self.agent_url)
            self._started_agent = False
            return True

        if not self.auto_start_agent:
            logger.warning("[demo_instance][resource] Resource Agent not reachable and auto-start disabled: %s", self.agent_url)
            return False

        cargo = shutil.which("cargo")
        cmd = [
            cargo or "cargo",
            "run",
            "--quiet",
            "--manifest-path",
            str(ROOT_DIR / "instance" / "resource_agent" / "Cargo.toml"),
            "--",
            "--listen",
            self.agent_listen,
            "--sample-interval-ms",
            str(self.sample_interval_ms),
            "--instance-id",
            runtime_instance_id,
        ]
        if cargo is None:
            logger.warning("[demo_instance][resource] cargo not found; command would be: %s", " ".join(cmd))
            return False

        stdout_tail: deque[str] = deque(maxlen=20)
        stderr_tail: deque[str] = deque(maxlen=20)
        logger.info("[demo_instance][resource] starting Resource Agent: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self._started_agent = True
        except Exception as exc:
            logger.warning("[demo_instance][resource] failed to start Resource Agent cmd=%s err=%s", " ".join(cmd), exc)
            return False

        asyncio.create_task(asyncio.to_thread(self._spawn_log_reader, self._proc.stdout, stdout_tail))
        asyncio.create_task(asyncio.to_thread(self._spawn_log_reader, self._proc.stderr, stderr_tail))
        if await self._wait_agent_ready(logger):
            return True

        returncode = self._proc.poll() if self._proc is not None else None
        logger.warning(
            "[demo_instance][resource] Resource Agent startup failed/timeout: cmd=%s returncode=%s stdout_tail=%r stderr_tail=%r",
            " ".join(cmd),
            returncode,
            self._read_tail(stdout_tail),
            self._read_tail(stderr_tail),
        )
        await self.stop(logger)
        return False

    async def _report_loop(self, runtime_instance_id: str, stop_event: asyncio.Event, logger) -> None:
        interval_s = max(0.001, self.report_interval_ms / 1000.0)
        logger.info(
            "[demo_instance][resource] reporting enabled: instance_id=%s agent_url=%s proxy_cp=%s interval_ms=%s timeout_s=%s",
            runtime_instance_id,
            self.agent_url,
            self.proxy_cp_url,
            self.report_interval_ms,
            self.report_timeout_s,
        )
        failures = 0
        first_ok = False
        while not stop_event.is_set():
            try:
                ok = await asyncio.to_thread(
                    report_once,
                    self.agent_url,
                    self.proxy_cp_url,
                    runtime_instance_id,
                    self.report_timeout_s,
                    False,
                    False,
                )
                if ok:
                    failures = 0
                    if not first_ok:
                        first_ok = True
                        logger.info("[demo_instance][resource] first resource report ok: instance_id=%s", runtime_instance_id)
                else:
                    failures += 1
                    if failures == 1 or failures % 30 == 0:
                        logger.warning("[demo_instance][resource] resource report rejected x%s instance_id=%s", failures, runtime_instance_id)
            except Exception as exc:
                failures += 1
                if failures == 1 or failures % 30 == 0:
                    logger.warning("[demo_instance][resource] resource report failed x%s err=%s", failures, exc)
            await asyncio.sleep(interval_s)

    async def start_after_registration(self, *, runtime_instance_id: str, stop_event: asyncio.Event, logger) -> None:
        if not self.enabled:
            logger.info("[demo_instance][resource] monitor disabled")
            return
        logger.info(
            "[demo_instance][resource] monitor enabled: agent_listen=%s agent_url=%s auto_start=%s report=%s",
            self.agent_listen,
            self.agent_url,
            self.auto_start_agent,
            self.report_enabled,
        )
        agent_ready = await self._start_or_reuse_agent(runtime_instance_id, logger)
        if not agent_ready:
            logger.warning("[demo_instance][resource] reporting skipped because Resource Agent is not ready")
            return
        if self.report_enabled:
            self._report_task = asyncio.create_task(self._report_loop(runtime_instance_id, stop_event, logger))
        else:
            logger.info("[demo_instance][resource] resource reporting disabled")

    def skip_after_registration_failure(self, logger) -> None:
        if self.enabled:
            logger.warning("[demo_instance][resource] reporting skipped because Instance registration to Proxy failed")

    async def stop(self, logger) -> None:
        if self._report_task is not None:
            self._report_task.cancel()
            self._report_task = None
        if not self._started_agent or self._proc is None or self._proc.poll() is not None:
            return
        try:
            logger.info("[demo_instance][resource] stopping Resource Agent process group pid=%s", self._proc.pid)
            os.killpg(self._proc.pid, signal.SIGTERM)
            await asyncio.to_thread(self._proc.wait, 3)
        except subprocess.TimeoutExpired:
            try:
                logger.warning("[demo_instance][resource] Resource Agent did not stop after SIGTERM; sending SIGKILL pid=%s", self._proc.pid)
                os.killpg(self._proc.pid, signal.SIGKILL)
            except Exception:
                pass
        except Exception as exc:
            logger.debug("[demo_instance][resource] Resource Agent cleanup ignored: %s", exc)


def main():
    # ====== Start Instance service ======
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
        dest="resource_monitor",
        action="store_true",
        default=_env_flag("INSTANCE_RESOURCE_MONITOR_ENABLE", config.INSTANCE_RESOURCE_MONITOR_ENABLE),
        help="enable Resource Agent startup and Proxy resource reporting (default: enabled for demo_instance)",
    )
    parser.add_argument(
        "--no-resource-monitor",
        dest="resource_monitor",
        action="store_false",
        help="disable Resource Agent startup and resource reporting",
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

    # Defaults come from config/env; command-line values override them
    cfg_port = int(os.environ.get("INSTANCE_PORT", config.INSTANCE_PORT))
    cfg_host = os.environ.get("INSTANCE_HOST", config.INSTANCE_HOST)

    host = args.host if args.host is not None else cfg_host
    port = args.port if args.port is not None else cfg_port

    # Ensure listening port == registered advertised port
    # instance_api.py lifespan reads INSTANCE_ADVERTISE_HOST/PORT and INSTANCE_PORT
    os.environ["INSTANCE_ADVERTISE_HOST"] = host
    os.environ["INSTANCE_ADVERTISE_PORT"] = str(port)
    os.environ["INSTANCE_PORT"] = str(port)
    os.environ["PROXY_CP_URL"] = args.proxy_cp_url.rstrip("/")
    if args.kdn_targets:
        os.environ["INSTANCE_TOPOLOGY_KDN_TARGETS"] = args.kdn_targets.strip()

    # (optional but strongly recommended)ensure each instance ID is unique to avoid pool upsert overwrite
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
        print("[demo_instance] resource monitor disabled", flush=True)

    from instance import instance
    instance.state._demo_resource_monitor = DemoResourceMonitor(  # type: ignore
        enabled=monitor_enabled,
        auto_start_agent=auto_start_agent,
        report_enabled=report_enabled,
        agent_listen=args.resource_agent_listen,
        agent_url=args.resource_agent_url,
        sample_interval_ms=args.resource_agent_sample_interval_ms,
        start_timeout_s=args.resource_agent_start_timeout_s,
        report_interval_ms=report_interval_ms,
        report_timeout_s=args.resource_report_timeout_s,
        proxy_cp_url=args.proxy_cp_url,
    )
    uvicorn.run(instance, host=host, port=port, reload=False)

if __name__ == "__main__":
    main()
