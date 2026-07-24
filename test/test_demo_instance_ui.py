import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path
import types
from collections import deque

import importlib.util
import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

spec = importlib.util.spec_from_file_location("demo_instance", os.path.join(os.path.dirname(__file__), "demo_instance.py"))
demo_instance = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(demo_instance)
config = demo_instance.config


def test_interval_from_hz_preserves_existing_conversion():
    assert demo_instance._interval_from_hz(1.0) == 1000
    assert demo_instance._interval_from_hz(2.0) == 500
    assert demo_instance._interval_from_hz(0.5) == 2000
    assert demo_instance._interval_from_hz(0.0) == 1000000
    assert demo_instance._interval_from_hz(0.0001) == 1000000


def test_ui_disabled_by_default(monkeypatch):
    monkeypatch.delenv("INSTANCE_UI_ENABLE", raising=False)
    args = demo_instance.parse_args([])
    assert args.ui_enabled is False


def test_ui_flags_and_env_precedence(monkeypatch):
    monkeypatch.setenv("INSTANCE_UI_ENABLE", "1")
    assert demo_instance.parse_args(["--no-ui"]).ui_enabled is False
    assert demo_instance.parse_args(["--ui"]).ui_enabled is True
    monkeypatch.setenv("INSTANCE_UI_LISTEN", "127.0.0.1:12345")
    monkeypatch.setenv("INSTANCE_UI_START_TIMEOUT_S", "9")
    args = demo_instance.parse_args(["--ui-listen", "127.0.0.1:23456", "--ui-start-timeout-s", "1.5"])
    assert args.ui_listen == "127.0.0.1:23456"
    assert args.ui_start_timeout_s == 1.5


def test_browser_open_resolution(monkeypatch):
    monkeypatch.delenv("INSTANCE_UI_OPEN_BROWSER", raising=False)
    assert demo_instance.parse_args(["--ui"]).ui_open_browser_resolved is True
    assert demo_instance.parse_args(["--ui", "--no-ui-open-browser"]).ui_open_browser_resolved is False
    assert demo_instance.parse_args(["--ui-open-browser"]).ui_open_browser_resolved is True
    monkeypatch.setenv("INSTANCE_UI_ENABLE", "1")
    monkeypatch.setenv("INSTANCE_UI_OPEN_BROWSER", "0")
    assert demo_instance.parse_args([]).ui_open_browser_resolved is False


def test_invalid_ui_listen_fails():
    with pytest.raises(SystemExit):
        demo_instance.parse_args(["--ui-listen", "bad"])


def test_dashboard_command_uses_runtime_values():
    d = demo_instance.DemoDashboard(
        enabled=True,
        listen="0.0.0.0:9202",
        open_browser=False,
        start_timeout_s=5,
        agent_listen="127.0.0.1:19201",
        sample_interval_ms=250,
    )
    cmd = d.build_command("runtime-id")
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("instance/resource_dashboard/dashboard_server.py")
    assert "--no-auto-start" in cmd
    assert cmd[cmd.index("--agent-listen") + 1] == "127.0.0.1:19201"
    assert cmd[cmd.index("--sample-interval-ms") + 1] == "250"
    assert cmd[cmd.index("--instance-id") + 1] == "runtime-id"
    assert cmd[cmd.index("--dashboard-listen") + 1] == "0.0.0.0:9202"


def test_dashboard_url_maps_wildcard_hosts():
    assert demo_instance.dashboard_url_for_listen("0.0.0.0:9202") == "http://127.0.0.1:9202"
    assert demo_instance.dashboard_url_for_listen("[::]:9202") == "http://127.0.0.1:9202"
    assert demo_instance.dashboard_url_for_listen("::1:9202") == "http://[::1]:9202"


class FakeProc:
    def __init__(self, poll_values=None):
        self.pid = 4321
        self.stdout = None
        self.stderr = None
        self.wait_calls = []
        self._poll_values = list(poll_values or [None])

    def poll(self):
        return self._poll_values[0] if self._poll_values else None

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if timeout == 3 and len(self.wait_calls) == 1:
            raise subprocess.TimeoutExpired("cmd", timeout)
        self._poll_values = [0]
        return 0


class Logger:
    def __init__(self):
        self.messages = []
    def info(self, *args): self.messages.append(("info", args))
    def warning(self, *args): self.messages.append(("warning", args))
    def debug(self, *args): self.messages.append(("debug", args))


class FakeHTTPResponse:
    def __init__(self, status=200, body=''):
        self.status = status
        self._body = body.encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def dashboard_payload(*, agent_url="http://127.0.0.1:9201", sample_interval_ms=1000, instance_id="runtime-id"):
    return {
        "ok": True,
        "dashboard": "ok",
        "agent": {
            "agent_url": agent_url,
            "sample_interval_ms": sample_interval_ms,
            "instance_id": instance_id,
        },
    }


def dashboard_for_health() -> object:
    return demo_instance.DemoDashboard(
        enabled=True,
        listen="127.0.0.1:9202",
        open_browser=False,
        start_timeout_s=0.1,
        agent_listen="127.0.0.1:9201",
        sample_interval_ms=1000,
    )


def test_health_ok_accepts_compatible_dashboard_json(monkeypatch):
    d = dashboard_for_health()
    monkeypatch.setattr(demo_instance.urllib.request, "urlopen", lambda req, timeout: FakeHTTPResponse(body=demo_instance.json.dumps(dashboard_payload())))
    assert d._health_ok(runtime_instance_id="runtime-id") is True


def test_health_ok_rejects_non_json_200(monkeypatch):
    d = dashboard_for_health()
    monkeypatch.setattr(demo_instance.urllib.request, "urlopen", lambda req, timeout: FakeHTTPResponse(body="not json"))
    assert d._health_ok(runtime_instance_id="runtime-id") is False


def test_health_ok_rejects_unrelated_200_service(monkeypatch):
    d = dashboard_for_health()
    monkeypatch.setattr(demo_instance.urllib.request, "urlopen", lambda req, timeout: FakeHTTPResponse(body=demo_instance.json.dumps({"ok": True})))
    assert d._health_ok(runtime_instance_id="runtime-id") is False


def test_health_ok_rejects_mismatched_agent_url(monkeypatch):
    d = dashboard_for_health()
    payload = dashboard_payload(agent_url="http://127.0.0.1:9999")
    monkeypatch.setattr(demo_instance.urllib.request, "urlopen", lambda req, timeout: FakeHTTPResponse(body=demo_instance.json.dumps(payload)))
    assert d._health_ok(runtime_instance_id="runtime-id") is False


def test_health_ok_rejects_mismatched_sample_interval(monkeypatch):
    d = dashboard_for_health()
    payload = dashboard_payload(sample_interval_ms=250)
    monkeypatch.setattr(demo_instance.urllib.request, "urlopen", lambda req, timeout: FakeHTTPResponse(body=demo_instance.json.dumps(payload)))
    assert d._health_ok(runtime_instance_id="runtime-id") is False


def test_health_ok_rejects_mismatched_instance_id(monkeypatch):
    d = dashboard_for_health()
    payload = dashboard_payload(instance_id="other-id")
    monkeypatch.setattr(demo_instance.urllib.request, "urlopen", lambda req, timeout: FakeHTTPResponse(body=demo_instance.json.dumps(payload)))
    assert d._health_ok(runtime_instance_id="runtime-id") is False



def test_reuse_existing_dashboard_does_not_spawn_or_stop(monkeypatch):
    calls = []
    d = demo_instance.DemoDashboard(enabled=True, listen="127.0.0.1:9202", open_browser=False, start_timeout_s=0.1, agent_listen="127.0.0.1:9201", sample_interval_ms=1000)
    monkeypatch.setattr(d, "_health_ok", lambda timeout_s=0.5, runtime_instance_id=None: True)
    monkeypatch.setattr(demo_instance.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    asyncio.run(d.start(runtime_instance_id="id", logger=Logger()))
    asyncio.run(d.stop(logger=Logger()))
    assert calls == []


def test_compatible_external_dashboard_reuse_validates_identity(monkeypatch):
    d = dashboard_for_health()
    popen_calls = []
    monkeypatch.setattr(demo_instance.urllib.request, "urlopen", lambda req, timeout: FakeHTTPResponse(body=demo_instance.json.dumps(dashboard_payload())))
    monkeypatch.setattr(demo_instance.subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k)))

    asyncio.run(d.start(runtime_instance_id="runtime-id", logger=Logger()))
    asyncio.run(d.stop(logger=Logger()))

    assert popen_calls == []
    assert d._started_dashboard is False


def test_incompatible_external_service_is_not_marked_reused(monkeypatch):
    d = dashboard_for_health()
    popen_calls = []
    log = Logger()
    monkeypatch.setattr(demo_instance.urllib.request, "urlopen", lambda req, timeout: FakeHTTPResponse(body=demo_instance.json.dumps({"ok": True})))

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        raise OSError("address already in use")

    monkeypatch.setattr(demo_instance.subprocess, "Popen", fake_popen)

    asyncio.run(d.start(runtime_instance_id="runtime-id", logger=log))

    assert popen_calls
    assert d._started_dashboard is False
    assert any(m[0] == "warning" and "failed to start Dashboard" in m[1][0] for m in log.messages)



def test_start_timeout_nonfatal_and_bounded_tails(monkeypatch):
    d = demo_instance.DemoDashboard(enabled=True, listen="127.0.0.1:9202", open_browser=False, start_timeout_s=0.01, agent_listen="127.0.0.1:9201", sample_interval_ms=1000)
    fake = FakeProc()
    d._stdout_tail = deque([f"out{i}\n" for i in range(30)], maxlen=20)
    d._stderr_tail = deque([f"err{i}\n" for i in range(30)], maxlen=20)
    monkeypatch.setattr(d, "_health_ok", lambda timeout_s=0.5, runtime_instance_id=None: False)
    monkeypatch.setattr(demo_instance.subprocess, "Popen", lambda *a, **k: fake)
    log = Logger()
    asyncio.run(d.start(runtime_instance_id="id", logger=log))
    assert d._started_dashboard is True
    assert "out0" not in demo_instance.DemoResourceMonitor._read_tail(d._stdout_tail)
    assert any(m[0] == "warning" for m in log.messages)


def test_browser_open_success_failure_and_disabled(monkeypatch):
    opened = []
    d = demo_instance.DemoDashboard(enabled=True, listen="127.0.0.1:9202", open_browser=True, start_timeout_s=0.1, agent_listen="127.0.0.1:9201", sample_interval_ms=1000)
    monkeypatch.setattr(d, "_health_ok", lambda timeout_s=0.5, runtime_instance_id=None: len(opened) == 0)
    monkeypatch.setattr(demo_instance.webbrowser, "open", lambda url: opened.append(url) or False)
    log = Logger()
    asyncio.run(d.start(runtime_instance_id="id", logger=log))
    assert opened == ["http://127.0.0.1:9202"]
    assert any(m[0] == "warning" for m in log.messages)


def test_cleanup_terminates_process_group_and_escalates(monkeypatch):
    d = demo_instance.DemoDashboard(enabled=True, listen="127.0.0.1:9202", open_browser=False, start_timeout_s=0.1, agent_listen="127.0.0.1:9201", sample_interval_ms=1000)
    d._proc = FakeProc()
    d._started_dashboard = True
    signals = []
    monkeypatch.setattr(demo_instance.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    asyncio.run(d.stop(logger=Logger()))
    assert signals == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]
    asyncio.run(d.stop(logger=Logger()))


def test_ui_disabled_creates_no_dashboard_process():
    d = demo_instance.DemoDashboard(enabled=False, listen=config.INSTANCE_UI_LISTEN, open_browser=False, start_timeout_s=1, agent_listen="127.0.0.1:9201", sample_interval_ms=1000)
    assert d.enabled is False
    assert d._proc is None


def test_main_default_report_interval_initialization_uses_hz_without_name_error(monkeypatch):
    fake_instance_app = types.SimpleNamespace(state=types.SimpleNamespace())
    fake_instance_module = types.ModuleType("instance")
    fake_instance_module.instance = fake_instance_app
    run_calls = []

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda *args, **kwargs: run_calls.append((args, kwargs))

    with monkeypatch.context() as m:
        m.setitem(sys.modules, "instance", fake_instance_module)
        m.setitem(sys.modules, "uvicorn", fake_uvicorn)
        m.setattr(sys, "argv", ["demo_instance.py", "--no-resource-monitor", "--no-ui"])
        m.delenv("INSTANCE_RESOURCE_REPORT_INTERVAL_MS", raising=False)
        m.delenv("INSTANCE_RESOURCE_REPORT_HZ", raising=False)
        m.delenv("INSTANCE_ID", raising=False)

        demo_instance.main()

        assert os.environ["INSTANCE_RESOURCE_REPORT_INTERVAL_MS"] == "1000"
        assert run_calls
        assert fake_instance_app.state._demo_resource_monitor.report_interval_ms == 1000
        assert fake_instance_app.state._demo_dashboard.enabled is False

    assert sys.modules.get("instance") is not fake_instance_module
    assert sys.modules.get("uvicorn") is not fake_uvicorn


def test_resource_agent_for_ui_uses_monitor_owner_without_reporting(monkeypatch):
    calls = []
    monitor = demo_instance.DemoResourceMonitor(
        enabled=True,
        auto_start_agent=True,
        report_enabled=True,
        agent_listen="127.0.0.1:9201",
        agent_url="http://127.0.0.1:9201",
        sample_interval_ms=1000,
        start_timeout_s=1,
        report_interval_ms=1000,
        report_timeout_s=1,
        proxy_cp_url="http://127.0.0.1:8002",
    )

    async def fake_start(runtime_instance_id, logger):
        calls.append((runtime_instance_id, logger))
        return True

    monkeypatch.setattr(monitor, "_start_or_reuse_agent", fake_start)
    asyncio.run(monitor.ensure_agent_for_ui(runtime_instance_id="runtime-id", logger=Logger()))

    assert calls and calls[0][0] == "runtime-id"
    assert monitor._report_task is None
