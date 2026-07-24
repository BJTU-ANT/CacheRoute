# CacheRoute Instance

The `instance/` directory contains the serving adapter between CacheRoute Proxy and a real or mocked LLM backend. An Instance receives OpenAI-compatible requests, exposes a small control plane for KVCache injection, registers with Proxy, and can optionally run local resource monitoring and a browser Resource Dashboard for debugging.

The Instance remains intentionally lightweight. Global route selection, knowledge placement, and text-versus-KVCache policy belong to Scheduler and Proxy.

## Role in CacheRoute

```text
Client
  -> Scheduler
      -> Proxy
          -> Instance
              -> vLLM / LMCache / Redis
```

The Instance is responsible for:

- serving Proxy-forwarded OpenAI-compatible requests;
- forwarding to vLLM or returning mock responses;
- registering, heartbeating, and unregistering with Proxy;
- exposing KVCache injection acknowledgement APIs;
- optionally probing Instance-to-KDN topology;
- optionally starting or reusing the Rust Resource Agent;
- optionally starting the browser Resource Dashboard;
- cleaning up only the Agent and Dashboard processes started by the demo process.

It is not responsible for:

- choosing a target Proxy or KDN;
- selecting an injection strategy;
- global or local resource-aware scheduling policy.

## Directory structure

```text
instance/
├── README.md
├── instance_api.py                  # service plane and FastAPI lifespan
├── control_plane.py                 # KVCache injection control plane
├── kv_service.py                    # KVCache injection/reuse helpers
├── mock_resp.py                     # mock OpenAI-compatible responses
├── pclient/
│   └── proxy_client.py              # Instance -> Proxy control-plane client
├── resource_agent/
│   ├── README.md
│   ├── Cargo.toml
│   ├── proxy_reporter.py
│   └── src/main.rs
├── resource_dashboard/
│   ├── README.md
│   ├── dashboard_app.py             # Tkinter desktop mode
│   ├── dashboard_server.py          # browser mode
│   └── static/
└── TTFT_predictor/
    ├── WORKFLOW.md
    ├── prefill_prediction_server.py
    ├── prefill_predictor.py
    ├── prefill_regressor.py
    └── request_generator.py
```

## Runtime planes and default ports

| Component | Default port | Purpose |
|---|---:|---|
| Instance service plane | `9001` | OpenAI-compatible inference endpoints. |
| Instance control plane | `9002` | KVCache injection notifications and control requests. |
| Rust Resource Agent | `9201` | CPU, memory, network, GPU, and admission-state snapshots. |
| Browser Resource Dashboard | `9202` | Local browser observability UI. |

The Resource Agent and Dashboard are auxiliary demo-owned processes. They are not in the inference request path.

## Service plane

`instance_api.py` exposes:

```text
POST /v1/chat/completions
POST /v1/completions
```

When `USE_MOCK=True`, responses come from `mock_resp.py`. In real serving mode, the Instance forwards requests to the configured vLLM-compatible endpoint.

## Startup lifecycle

A normal demo startup uses `test/demo_instance.py`:

```text
demo_instance.py
  -> resolve CLI > environment > config/default values
  -> set advertised Instance host, port, and runtime Instance ID
  -> construct DemoResourceMonitor
  -> construct DemoDashboard when UI is enabled
  -> import the Instance FastAPI app
  -> run Uvicorn
```

During the FastAPI lifespan, `instance_api.py` performs:

```text
Instance lifespan
  -> start the local Instance control plane
  -> attempt Proxy registration
  -> start heartbeat handling
  -> if registration succeeds:
       start/reuse Resource Agent
       start resource reporting when enabled
  -> if registration fails and UI is enabled:
       start/reuse the local Resource Agent for Dashboard use
       skip Proxy resource reporting
  -> start the Dashboard when enabled
  -> optionally start topology discovery
  -> serve requests
  -> shutdown:
       stop Dashboard
       stop resource reporting
       stop only the demo-owned Resource Agent
       stop heartbeat/control-plane tasks and close clients
```

Important behavior:

- Dashboard startup is **not gated on successful Proxy registration**.
- Resource reporting to Proxy still requires a successfully registered Instance.
- Dashboard, browser-opening, and Agent startup failures are logged as warnings and do not abort the Instance whenever local serving can continue.
- Integrated Dashboard mode always passes `--no-auto-start` to `dashboard_server.py`, so `demo_instance.py` remains the Resource Agent lifecycle owner.

The default runtime Instance ID is:

```text
hp_<host>:<port>
```

For example:

```text
hp_127.0.0.1:9001
```

## Quick start

Start Proxy first when registration and resource reporting are part of the test:

```bash
cd test
python3 demo_proxy.py \
  --host 127.0.0.1 \
  --port 8001 \
  --strategy round_robin \
  --injection-strategy iws
```

Start an Instance without the Dashboard:

```bash
cd test
python3 demo_instance.py \
  --host 127.0.0.1 \
  --port 9001 \
  --proxy-cp-url http://127.0.0.1:8002
```

Start the Instance and browser Resource Dashboard with one command:

```bash
cd test
python3 demo_instance.py \
  --host 127.0.0.1 \
  --port 9001 \
  --proxy-cp-url http://127.0.0.1:8002 \
  --ui
```

The default Dashboard URL is:

```text
http://127.0.0.1:9202
```

For SSH sessions, containers, CI, or machines without a usable local browser:

```bash
python3 demo_instance.py \
  --host 127.0.0.1 \
  --port 9001 \
  --ui \
  --no-ui-open-browser
```

`--no-ui-open-browser` does **not** disable the Dashboard. It only disables the call that opens a browser automatically. Open the printed URL manually.

Disable the integrated Dashboard completely with:

```bash
python3 demo_instance.py --no-ui
```

## Remote and container access

### SSH port forwarding

When the Instance runs on a remote host and the Dashboard listens on loopback:

```bash
ssh -L 9202:127.0.0.1:9202 user@server
```

Then open locally:

```text
http://127.0.0.1:9202
```

### Docker without host networking

Publish the Dashboard port:

```bash
-p 9202:9202
```

and listen on all container interfaces:

```bash
--ui-listen 0.0.0.0:9202
```

A wildcard listen address is converted to `127.0.0.1` only for the local health-check and browser URL. The server still listens on `0.0.0.0`.

## Configuration

Defaults are defined in `core/config.py`.

Common Instance settings:

```python
INSTANCE_BASE_URL = "http://127.0.0.1:9001"
INSTANCE_HOST = "127.0.0.1"
INSTANCE_PORT = 9001
INSTANCE_CP_HOST = "127.0.0.1"
INSTANCE_CP_PORT = 9002
VLLM_BASE_URL = "http://127.0.0.1:8000"
USE_MOCK = False
```

Resource-monitoring settings:

```python
INSTANCE_RESOURCE_MONITOR_ENABLE = True
INSTANCE_RESOURCE_AUTO_START_AGENT = True
INSTANCE_RESOURCE_AGENT_LISTEN = "127.0.0.1:9201"
INSTANCE_RESOURCE_AGENT_URL = "http://127.0.0.1:9201"
INSTANCE_RESOURCE_AGENT_SAMPLE_INTERVAL_MS = 1000
INSTANCE_RESOURCE_AGENT_START_TIMEOUT_S = 60.0
INSTANCE_RESOURCE_REPORT_ENABLE = False
INSTANCE_RESOURCE_REPORT_HZ = 1.0
INSTANCE_RESOURCE_REPORT_INTERVAL_MS = 1000
INSTANCE_RESOURCE_REPORT_TIMEOUT_S = 2.0
```

Integrated Dashboard settings:

```python
INSTANCE_UI_ENABLE = False
INSTANCE_UI_LISTEN = "0.0.0.0:9202"
INSTANCE_UI_OPEN_BROWSER = False
INSTANCE_UI_START_TIMEOUT_S = 5.0
```

Environment variables use the same names. Explicit command-line options override environment values, which override config/default values.

## `demo_instance.py` CLI

Common options:

| Option | Description |
|---|---|
| `--host` | Instance service-plane bind host. |
| `--port` | Instance service-plane bind port. |
| `--proxy-cp-url` | Proxy control-plane URL. |
| `--kdn-targets` | Optional KDN topology-discovery targets. |

Resource-monitoring options:

| Option | Description |
|---|---|
| `--resource-monitor` / `--no-resource-monitor` | Enable or disable the demo resource-monitor path. |
| `--resource-agent` / `--no-resource-agent` | Enable or disable automatic Rust Agent startup. |
| `--resource-report` / `--no-resource-report` | Enable or disable Proxy resource reporting. |
| `--resource-agent-listen` | Resource Agent listen address. |
| `--resource-agent-url` | Resource Agent base URL used by the reporter. |
| `--resource-agent-sample-interval-ms` | Agent sample interval. |
| `--resource-agent-start-timeout-s` | Agent readiness timeout. |
| `--resource-report-hz` | Report frequency when no explicit interval is set. |
| `--resource-report-interval-ms` | Explicit report interval; overrides Hz. |
| `--resource-report-timeout-s` | Snapshot/report HTTP timeout. |

Dashboard options:

| Option | Description |
|---|---|
| `--ui` / `--no-ui` | Enable or disable integrated browser Dashboard startup. |
| `--ui-listen` | Dashboard listen address, for example `0.0.0.0:9202`. |
| `--ui-open-browser` / `--no-ui-open-browser` | Enable or disable automatic browser opening. |
| `--ui-start-timeout-s` | Dashboard readiness timeout. |

Explicit `--ui` enables browser opening by default unless `--no-ui-open-browser` is also supplied. UI enabled only through environment/config uses `INSTANCE_UI_OPEN_BROWSER`.

## Validate the integrated Dashboard

Keep `demo_instance.py` running and use another terminal:

```bash
curl -fsS http://127.0.0.1:9202/api/health | python3 -m json.tool
curl -fsS http://127.0.0.1:9202/ | head
curl -fsS http://127.0.0.1:9201/healthz | python3 -m json.tool
```

The Dashboard health response should identify the same Agent configuration and runtime Instance ID used by `demo_instance.py`, for example:

```json
{
  "ok": true,
  "dashboard": "ok",
  "agent": {
    "agent_url": "http://127.0.0.1:9201",
    "sample_interval_ms": 1000,
    "instance_id": "hp_127.0.0.1:9001"
  }
}
```

Check the managed Dashboard command:

```bash
pgrep -af 'resource_dashboard/dashboard_server.py'
```

Integrated mode should include:

```text
--no-auto-start
```

On `Ctrl+C`, the Dashboard should stop before the demo-owned Resource Agent is cleaned up.

## Proxy registration and resource reporting

The Instance calls the Proxy control plane through `instance/pclient/proxy_client.py`:

```text
POST /v1/instance/register
POST /v1/instance/heartbeat
POST /v1/instance/unregister
```

The demo Resource Agent path is:

```text
test/demo_instance.py
  -> start or reuse Rust Resource Agent
  -> wait for /healthz
  -> fetch /v1/resource/snapshot
  -> after successful Proxy registration, report to:
       Proxy /v1/instance/resource_snapshot
```

Inspect Proxy-side state with:

```bash
curl -sS http://127.0.0.1:8002/debug/instance_resources | python3 -m json.tool
curl -sS 'http://127.0.0.1:8002/v1/instance/list?include_dead=true' | python3 -m json.tool
```

The Proxy currently stores this resource data for observation; it is not yet used for Instance selection.

## KVCache injection control plane

The Instance control plane receives KVCache injection notifications from Proxy. The high-level path is:

```text
Proxy prepare queue
  -> classify knowledge
  -> reserve KDN-to-Instance transfer
  -> POST /v1/kv/inject_ready
  -> wait for acknowledgement
  -> forward the request to the Instance service plane
```

`kv_service.py` contains local KVCache injection/reuse helpers. Actual Redis, LMCache, and vLLM behavior depends on the deployment environment.

## Standalone Resource Agent and Dashboard modes

The Rust Agent can be started manually:

```bash
cargo run --manifest-path instance/resource_agent/Cargo.toml -- \
  --listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

The Dashboard also has standalone browser and Tkinter desktop modes. Use them for component-level debugging; use `demo_instance.py --ui` for normal integrated demos. See [`resource_dashboard/README.md`](resource_dashboard/README.md).

Tkinter is required only for `dashboard_app.py`. The browser Dashboard and `demo_instance.py --ui` do not require Tkinter or X11.

## TTFT predictor

`instance/TTFT_predictor/` contains an experimental prefill/TTFT prediction subsystem. It is separate from the Resource Agent path:

- Resource Agent observes host and device state.
- TTFT predictor estimates model prefill latency.

See `instance/TTFT_predictor/WORKFLOW.md` for call paths and warmup behavior.

## Tests

Focused integrated-Dashboard tests:

```bash
python3 -m pytest -q test/test_demo_instance_ui.py
```

End-to-end resource-monitor smoke test:

```bash
python3 test/demo_resource_monitor_e2e.py \
  --agent-listen 127.0.0.1:19201 \
  --agent-url http://127.0.0.1:19201
```

Use non-default ports to avoid reusing stale Agent or Dashboard processes.

## Troubleshooting

### Dashboard starts but no window appears

- `--no-ui-open-browser` intentionally suppresses automatic browser opening.
- Root users, containers, SSH sessions, and headless servers may not have a usable browser even with `--ui`.
- Open the printed URL manually or use SSH port forwarding.

### Proxy registration fails

The Instance and local Dashboard may continue running. Proxy resource reporting is skipped until the Instance can register successfully.

### Port `9201` or `9202` is already in use

Choose alternate ports:

```bash
python3 test/demo_instance.py \
  --resource-agent-listen 127.0.0.1:19201 \
  --resource-agent-url http://127.0.0.1:19201 \
  --ui \
  --ui-listen 127.0.0.1:19202
```

### `cargo: command not found`

Install a recent stable Rust/Cargo toolchain or use the CacheRoute development image that includes Rust.

### GPU list is empty

Run `nvidia-smi` and confirm Docker was started with GPU access such as `--gpus all`.

## Current status

Implemented:

- Instance service and control planes;
- Proxy registration and heartbeat;
- KVCache injection signalling;
- demo-managed Resource Agent startup, reuse, reporting, and cleanup;
- optional integrated browser Resource Dashboard;
- Dashboard identity/configuration validation before reuse;
- experimental TTFT prediction.

Not yet implemented:

- resource-aware Proxy Instance selection;
- detailed vLLM runtime queue metrics;
- detailed KVCache block residency metrics;
- lower-overhead GPU collection through NVML or a similar API.
