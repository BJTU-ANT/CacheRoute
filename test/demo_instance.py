import argparse
import json
import os
import sys
import asyncio
import signal
import subprocess
import shutil
import time
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

import importlib.util

_config_spec = importlib.util.spec_from_file_location("cacheroute_core_config", ROOT_DIR / "core" / "config.py")
config = importlib.util.module_from_spec(_config_spec)
assert _config_spec.loader is not None
_config_spec.loader.exec_module(config)

_reporter_spec = importlib.util.spec_from_file_location("cacheroute_proxy_reporter", ROOT_DIR / "instance" / "resource_agent" / "proxy_reporter.py")
_reporter = importlib.util.module_from_spec(_reporter_spec)
assert _reporter_spec.loader is not None
_reporter_spec.loader.exec_module(_reporter)
report_once = _reporter.report_once


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _set_bool_env(name: str, enabled: bool) -> None:
    os.environ[name] = "1" if enabled else "0"


def _interval_from_hz(hz: float) -> int:
    return max(1, int(round(1000.0 / max(float(hz), 0.001))))


def parse_listen(value: str) -> tuple[str, int]:
    raw = (value or "").strip()
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0 or end + 1 >= len(raw) or raw[end + 1] != ":":
            raise argparse.ArgumentTypeError(f"listen address must be host:port, got {value!r}")
        host = raw[1:end]
        port_s = raw[end + 2:]
    else:
        if ":" not in raw:
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


def _reachable_host(host: str) -> str:
    normalized = (host or "").strip().strip("[]")
    if normalized in {"0.0.0.0", "::", ""}:
        return "127.0.0.1"
    return normalized


def _url_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def dashboard_url_for_listen(listen: str) -> str:
    host, port = parse_listen(listen)
    return f"http://{_url_host(_reachable_host(host))}:{port}"


def resolve_ui_options(args: argparse.Namespace) -> argparse.Namespace:
    env_ui = _env_flag("INSTANCE_UI_ENABLE", config.INSTANCE_UI_ENABLE)
    ui_enabled = env_ui if args.ui is None else bool(args.ui)

    env_browser = _env_flag("INSTANCE_UI_OPEN_BROWSER", config.INSTANCE_UI_OPEN_BROWSER)
    if args.ui_open_browser is None:
        open_browser = True if args.ui is True else env_browser
    else:
        open_browser = bool(args.ui_open_browser)

    args.ui_enabled = ui_enabled
    args.ui_open_browser_resolved = open_browser
    parse_listen(args.ui_listen)
    if float(args.ui_start_timeout_s) < 0:
        raise argparse.ArgumentTypeError("--ui-start-timeout-s must be non-negative")
    return args


class DemoDashboard:
    """Own browser Dashboard lifecycle for demo_instance.py integrated UI only."""

    def __init__(self, *, enabled: bool, listen: str, open_browser: bool, start_timeout_s: float, agent_listen: str, sample_interval_ms: int) -> None:
        self.enabled = enabled
        self.listen = listen
        self.open_browser = open_browser
        self.start_timeout_s = float(start_timeout_s)
        self.agent_listen = agent_listen
        self.sample_interval_ms = int(sample_interval_ms)
        self._proc = None
        self._started_dashboard = False
        self._stdout_tail: deque[str] = deque(maxlen=20)
        self._stderr_tail: deque[str] = deque(maxlen=20)

    @property
    def url(self) -> str:
        return dashboard_url_for_listen(self.listen)

    def build_command(self, runtime_instance_id: str) -> list[str]:
        return [
            sys.executable,
            str((ROOT_DIR / "instance" / "resource_dashboard" / "dashboard_server.py").resolve()),
            "--dashboard-listen", self.listen,
            "--agent-listen", self.agent_listen,
            "--sample-interval-ms", str(self.sample_interval_ms),
            "--instance-id", runtime_instance_id,
            "--no-auto-start",
        ]

    def _expected_agent_url(self) -> str:
        return f"http://{self.agent_listen}"

    def _compatible_health_payload(self, payload: dict, runtime_instance_id: str | None = None) -> bool:
        if payload.get("ok") is not True or payload.get("dashboard") != "ok":
            return False
        agent = payload.get("agent")
        if not isinstance(agent, dict):
            return False
        if agent.get("agent_url") != self._expected_agent_url():
            return False
        if int(agent.get("sample_interval_ms", -1)) != self.sample_interval_ms:
            return False
        if runtime_instance_id is not None and agent.get("instance_id") != runtime_instance_id:
            return False
        return True

    def _health_ok(self, timeout_s: float = 0.5, runtime_instance_id: str | None = None) -> bool:
        try:
            req = urllib.request.Request(f"{self.url}/api/health", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                if not (200 <= int(resp.status) < 300):
                    return False
                body = resp.read().decode("utf-8")
            payload = json.loads(body) if body else {}
            return isinstance(payload, dict) and self._compatible_health_payload(payload, runtime_instance_id)
        except Exception:
            return False

    async def _wait_ready(self, logger, runtime_instance_id: str) -> bool:
        deadline = time.monotonic() + max(0.0, self.start_timeout_s)
        while time.monotonic() <= deadline:
            if await asyncio.to_thread(self._health_ok, 0.5, runtime_instance_id):
                logger.info("[demo_instance][ui] dashboard ready: %s", self.url)
                return True
            if self._proc is not None and self._proc.poll() is not None:
                return False
            await asyncio.sleep(0.2)
        return False

    async def _open_browser(self, logger) -> None:
        try:
            ok = await asyncio.to_thread(webbrowser.open, self.url)
            if not ok:
                logger.warning("[demo_instance][ui] browser open returned false for %s; use --no-ui-open-browser in headless environments", self.url)
        except Exception as exc:
            logger.warning("[demo_instance][ui] browser open failed for %s: %s", self.url, exc)

    async def start(self, *, runtime_instance_id: str, logger) -> None:
        if not self.enabled:
            return
        cmd = self.build_command(runtime_instance_id)
        if await asyncio.to_thread(self._health_ok, 0.5, runtime_instance_id):
            logger.info("[demo_instance][ui] reusing compatible reachable Dashboard: %s", self.url)
            if self.open_browser:
                await self._open_browser(logger)
            return
        logger.info("[demo_instance][ui] starting Dashboard: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(cmd, cwd=str(ROOT_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            self._started_dashboard = True
        except Exception as exc:
            logger.warning("[demo_instance][ui] failed to start Dashboard cmd=%s err=%s", " ".join(cmd), exc)
            return
        asyncio.create_task(asyncio.to_thread(DemoResourceMonitor._spawn_log_reader, self._proc.stdout, self._stdout_tail))
        asyncio.create_task(asyncio.to_thread(DemoResourceMonitor._spawn_log_reader, self._proc.stderr, self._stderr_tail))
        if await self._wait_ready(logger, runtime_instance_id):
            print(f"[demo_instance] Resource Dashboard URL: {self.url}", flush=True)
            if self.open_browser:
                await self._open_browser(logger)
            return
        returncode = self._proc.poll() if self._proc is not None else None
        reason = "exited before readiness" if returncode is not None else f"readiness timeout after {self.start_timeout_s}s"
        logger.warning(
            "[demo_instance][ui] Dashboard startup failed: reason=%s cmd=%s returncode=%s stdout_tail=%r stderr_tail=%r",
            reason, " ".join(cmd), returncode, DemoResourceMonitor._read_tail(self._stdout_tail), DemoResourceMonitor._read_tail(self._stderr_tail),
        )

    async def stop(self, logger) -> None:
        if not self._started_dashboard or self._proc is None:
            return
        proc = self._proc
        if proc.poll() is not None:
            try:
                await asyncio.to_thread(proc.wait, 0)
            except Exception:
                pass
            self._started_dashboard = False
            return
        try:
            logger.info("[demo_instance][ui] stopping Dashboard process group pid=%s", proc.pid)
            os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.to_thread(proc.wait, 3)
        except subprocess.TimeoutExpired:
            try:
                logger.warning("[demo_instance][ui] Dashboard did not stop after SIGTERM; sending SIGKILL pid=%s", proc.pid)
                os.killpg(proc.pid, signal.SIGKILL)
                await asyncio.to_thread(proc.wait, 3)
            except Exception:
                pass
        except Exception as exc:
            logger.debug("[demo_instance][ui] Dashboard cleanup ignored: %s", exc)
        finally:
            self._started_dashboard = False


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

    async def ensure_agent_for_ui(self, *, runtime_instance_id: str, logger) -> None:
        if not self.enabled:
            logger.warning("[demo_instance][ui] resource monitor disabled; Dashboard will reuse external agent if reachable")
            return
        await self._start_or_reuse_agent(runtime_instance_id, logger)

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


def build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument("--ui", dest="ui", action="store_true", default=None, help="auto-start the browser Resource Dashboard")
    parser.add_argument("--no-ui", dest="ui", action="store_false", help="disable browser Resource Dashboard startup")
    parser.add_argument(
        "--ui-listen",
        type=str,
        default=os.environ.get("INSTANCE_UI_LISTEN", config.INSTANCE_UI_LISTEN),
        help="Resource Dashboard listen address (host:port)",
    )
    parser.add_argument("--ui-open-browser", dest="ui_open_browser", action="store_true", default=None, help="open the Resource Dashboard URL after readiness")
    parser.add_argument("--no-ui-open-browser", dest="ui_open_browser", action="store_false", help="do not open a browser for the Resource Dashboard")
    parser.add_argument(
        "--ui-start-timeout-s",
        type=float,
        default=float(os.environ.get("INSTANCE_UI_START_TIMEOUT_S", config.INSTANCE_UI_START_TIMEOUT_S)),
        help="seconds to wait for Resource Dashboard readiness",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return resolve_ui_options(args)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))


def main():
    # ====== Start Instance service ======
    args = parse_args()

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
    _set_bool_env("INSTANCE_UI_ENABLE", args.ui_enabled)
    _set_bool_env("INSTANCE_UI_OPEN_BROWSER", args.ui_open_browser_resolved)
    os.environ["INSTANCE_UI_LISTEN"] = args.ui_listen
    os.environ["INSTANCE_UI_START_TIMEOUT_S"] = str(args.ui_start_timeout_s)

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
    if args.ui_enabled:
        print(
            "[demo_instance] resource dashboard enabled: "
            f"listen={args.ui_listen} url={dashboard_url_for_listen(args.ui_listen)} "
            f"open_browser={args.ui_open_browser_resolved} timeout_s={args.ui_start_timeout_s}",
            flush=True,
        )
    else:
        print("[demo_instance] resource dashboard disabled", flush=True)

    import uvicorn
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
    instance.state._demo_dashboard = DemoDashboard(  # type: ignore
        enabled=args.ui_enabled,
        listen=args.ui_listen,
        open_browser=args.ui_open_browser_resolved,
        start_timeout_s=args.ui_start_timeout_s,
        agent_listen=args.resource_agent_listen,
        sample_interval_ms=args.resource_agent_sample_interval_ms,
    )
    uvicorn.run(instance, host=host, port=port, reload=False)

if __name__ == "__main__":
    main()
