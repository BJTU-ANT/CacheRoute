# CacheRoute Instance

The `instance/` directory contains the local serving adapter that sits between the Proxy and a real or mocked LLM backend. An Instance receives OpenAI-compatible requests from the Proxy, exposes a small control plane for KVCache injection, registers itself with the Proxy control plane, and can report local resource snapshots through the Rust Resource Agent demo path.

The Instance is intentionally lightweight: it adapts request/response formats, maintains control-plane connectivity, and delegates model execution to vLLM or a mock backend. It does not choose global routes and it does not own Scheduler-level policy.

---

## Role in CacheRoute

```text
Client
  -> Scheduler
      -> Proxy
          -> Instance
              -> vLLM / LMCache / Redis
```

The Instance is the last CacheRoute component before the actual inference backend.

It is responsible for:

- exposing OpenAI-compatible service endpoints for the Proxy;
- forwarding requests to vLLM when real serving is enabled;
- providing mock responses for local control-plane validation;
- registering, heartbeating, and unregistering with the local Proxy;
- exposing an Instance control plane for KVCache injection acknowledgement;
- optionally probing Instance-to-KDN topology;
- running demo-managed resource monitoring through the Rust Resource Agent path.

It is not responsible for:

- choosing the target Proxy;
- selecting KDN servers;
- deciding text vs KVCache injection policy;
- resource-aware Instance selection inside the Proxy.

Those decisions live in Scheduler and Proxy layers.

---

## Directory Structure

```text
instance/
├── README.md
├── instance_api.py                  # Instance service plane and startup lifecycle
├── control_plane.py                 # Instance control-plane API for KVCache injection
├── kv_service.py                    # KVCache injection / reuse helper logic
├── mock_resp.py                     # Mock OpenAI-compatible responses for local testing
├── pclient/
│   └── proxy_client.py              # Instance -> Proxy control-plane client
├── resource_agent/
│   ├── README.md
│   ├── Cargo.toml
│   ├── proxy_reporter.py            # Standalone snapshot reporter and shared report helper
│   └── src/main.rs                  # Native Rust Resource Agent
├── resource_dashboard/
│   ├── README.md
│   ├── dashboard_app.py             # Tkinter local dashboard
│   ├── dashboard_server.py          # Browser dashboard fallback
│   └── static/
└── TTFT_predictor/
    ├── WORKFLOW.md
    ├── prefill_prediction_server.py
    ├── prefill_predictor.py
    ├── prefill_regressor.py
    └── request_generator.py
```

---

## Runtime Planes

The Instance has two HTTP planes.

| Plane | Default Port | Main File | Purpose |
|---|---:|---|---|
| Service plane | `9001` | `instance_api.py` | Receives Proxy-forwarded OpenAI-compatible inference requests. |
| Control plane | `9002` | `control_plane.py` | Receives KVCache injection notifications and local control requests. |

The Rust Resource Agent is a separate demo-owned sidecar, not part of the normal request path.

| Component | Default Port | Purpose |
|---|---:|---|
| Rust Resource Agent | `9201` | Exposes CPU, memory, network, GPU, and admission-state snapshots. |
| Resource Dashboard server | `9202` | Browser dashboard for local inspection. |

---

## Service Plane

`instance_api.py` exposes OpenAI-compatible endpoints:

```text
POST /v1/chat/completions
POST /v1/completions
```

The Proxy forwards requests to these endpoints after knowledge preparation and Instance selection.

### Chat completion

For `POST /v1/chat/completions`, the Instance supports both streaming and non-streaming behavior.

When `USE_MOCK=True`, it returns mock responses from `mock_resp.py`. When real vLLM mode is enabled, it forwards the body to:

```text
<VLLM_BASE_URL>/v1/chat/completions
```

and streams or returns the upstream response.

### Completion

For `POST /v1/completions`, the Instance follows the same adapter pattern and forwards to the vLLM-compatible completion endpoint when mock mode is disabled.

---

## Startup Lifecycle

A normal demo startup uses `test/demo_instance.py`, which sets environment variables and then imports `instance.instance_api`.

```text
demo_instance.py
  -> resolve CLI/env/config
  -> set INSTANCE_ADVERTISE_HOST / INSTANCE_ADVERTISE_PORT
  -> create DemoResourceMonitor if enabled
  -> import instance_api
  -> uvicorn.run(instance)
```

During the FastAPI lifespan in `instance_api.py`, the Instance performs:

```text
Instance lifespan
  -> start local Instance control plane
  -> register with Proxy control plane
  -> start heartbeat loop
  -> start demo resource monitoring after successful registration
  -> optionally run Instance-to-KDN topology discovery
  -> serve requests
  -> on shutdown: stop heartbeat, unregister, close client, stop control plane, clean demo-owned agent
```

The registration target is configured by:

```text
PROXY_CP_URL = http://127.0.0.1:8002
```

The advertised Instance address is controlled by:

```text
INSTANCE_ADVERTISE_HOST
INSTANCE_ADVERTISE_PORT
```

`test/demo_instance.py` keeps the advertised address aligned with the actual bind address by default.

---

## Proxy Registration and Heartbeat

The Instance uses `instance/pclient/proxy_client.py` to call the Proxy control plane.

Registration:

```text
POST /v1/instance/register
```

Heartbeat:

```text
POST /v1/instance/heartbeat
```

Unregister:

```text
POST /v1/instance/unregister
```

The default runtime Instance ID is:

```text
hp_<host>:<port>
```

For example:

```text
hp_127.0.0.1:9001
```

If Proxy registration fails, the Instance service can keep running for local debugging, but resource reporting to Proxy is skipped because the Proxy would reject reports from an unknown Instance.

---

## KVCache Injection Control Plane

The Instance control plane is started automatically during `instance_api.py` lifespan.

The Proxy uses this path when it decides to use KVCache injection. The high-level flow is:

```text
Proxy prepare queue
  -> classify knowledge as kv_ready / text_only / miss
  -> reserve KDN-to-Instance KV transfer
  -> call Instance control plane
      POST /v1/kv/inject_ready
  -> wait for acknowledgement
  -> move task to ready queue
  -> forward request to Instance service plane
```

The Instance control plane receives metadata such as:

```text
request_id
kdn_addr
model
knowledge_ids
```

`kv_service.py` contains the local helper logic for KVCache injection / reuse behavior. The exact Redis and LMCache behavior depends on the runtime environment and the downstream vLLM + LMCache setup.

---

## Resource Monitoring Path

The current resource-monitoring path was added for demo observability and later scheduling integration.

The demo path is:

```text
test/demo_instance.py
  -> start or reuse Rust Resource Agent
  -> wait for /healthz
  -> after Proxy registration succeeds, periodically fetch /v1/resource/snapshot
  -> report snapshot to Proxy control plane
  -> Proxy stores normalized fields in InstancePool.resource
```

Proxy-side APIs for inspection:

```bash
curl -sS "http://127.0.0.1:8002/debug/instance_resources" | python3 -m json.tool
curl -sS "http://127.0.0.1:8002/v1/instance/list?include_dead=true" | python3 -m json.tool
```

The resource snapshot currently includes:

- CPU utilization and load averages;
- memory used/free/total;
- network RX/TX throughput;
- optional GPU utilization, memory, temperature, and power;
- admission-state hints;
- collection and reporting timestamps.

The Proxy stores resource data for observation only. It does not yet use these fields for Instance selection.

### Resource report metadata

`proxy_reporter.py` includes report metadata so the Proxy can distinguish fresh data from stale data:

```text
reported_instance_id
report_monotonic_ms
report_wall_time_ms
agent_snapshot_timestamp_ms
```

The Proxy normalizes these into `InstancePool.resource` fields such as:

```text
resource_ts_ms
resource_reported_at
resource_report_monotonic_ms
resource_report_wall_time_ms
reported_instance_id
```

---

## Quick Start

Start the Proxy first:

```bash
cd test
python3 demo_proxy.py \
  --host 127.0.0.1 \
  --port 8001 \
  --strategy round_robin \
  --injection-strategy iws
```

Start one Instance:

```bash
cd test
python3 demo_instance.py \
  --host 127.0.0.1 \
  --port 9001 \
  --proxy-cp-url http://127.0.0.1:8002
```

By default, `demo_instance.py` enables resource monitoring for the demo path. It starts or reuses the Rust Resource Agent at `127.0.0.1:9201`, waits for readiness, and starts reporting only after Proxy registration succeeds.

Disable resource monitoring when needed:

```bash
python3 demo_instance.py \
  --host 127.0.0.1 \
  --port 9001 \
  --proxy-cp-url http://127.0.0.1:8002 \
  --no-resource-monitor
```

Use a non-default Resource Agent port:

```bash
python3 demo_instance.py \
  --host 127.0.0.1 \
  --port 9001 \
  --proxy-cp-url http://127.0.0.1:8002 \
  --resource-agent-listen 127.0.0.1:19201 \
  --resource-agent-url http://127.0.0.1:19201
```

---

## Configuration

Most defaults are defined in `core/config.py`.

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

`demo_instance.py` enables reporting when resource monitoring is enabled, even though the base config keeps `INSTANCE_RESOURCE_REPORT_ENABLE=False` to avoid surprising non-demo imports.

---

## CLI Options in `demo_instance.py`

Common options:

| Option | Description |
|---|---|
| `--host` | Instance service-plane bind host. |
| `--port` | Instance service-plane bind port. |
| `--proxy-cp-url` | Proxy control-plane URL for registration and resource reporting. |
| `--kdn-targets` | Optional KDN targets for topology discovery. |

Resource-monitoring options:

| Option | Description |
|---|---|
| `--resource-monitor` / `--no-resource-monitor` | Enable or disable the full demo resource monitor path. |
| `--resource-agent` / `--no-resource-agent` | Auto-start or do not auto-start the Rust Resource Agent. |
| `--resource-report` / `--no-resource-report` | Enable or disable resource reporting to Proxy. |
| `--resource-agent-listen` | Rust agent listen address, such as `127.0.0.1:9201`. |
| `--resource-agent-url` | Base URL used by the reporter. |
| `--resource-agent-sample-interval-ms` | Rust agent sampling interval. |
| `--resource-agent-start-timeout-s` | Time to wait for `/healthz`. |
| `--resource-report-hz` | Report frequency when interval is not explicitly set. |
| `--resource-report-interval-ms` | Explicit report interval. Overrides Hz. |
| `--resource-report-timeout-s` | HTTP timeout for snapshot fetch and Proxy report. |

---

## Resource Agent Standalone Mode

The Rust agent can still be run manually:

```bash
cargo run --manifest-path instance/resource_agent/Cargo.toml -- \
  --listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

Validate it directly:

```bash
curl -sS http://127.0.0.1:9201/healthz
curl -sS http://127.0.0.1:9201/v1/resource/snapshot | python3 -m json.tool
```

The standalone Python reporter remains available:

```bash
python3 instance/resource_agent/proxy_reporter.py \
  --agent-url http://127.0.0.1:9201 \
  --proxy-cp-url http://127.0.0.1:8002 \
  --instance-id hp_127.0.0.1:9001 \
  --once
```

---

## Resource Dashboard

For local resource visualization, use:

```text
instance/resource_dashboard/
```

Two dashboard modes are available:

| Mode | File | Use case |
|---|---|---|
| Desktop | `dashboard_app.py` | Local machine with a GUI display. |
| Browser | `dashboard_server.py` | Containers, remote machines, and headless servers. |

The dashboard is a local observability tool. It can start or connect to the Rust Resource Agent, but it is separate from Proxy resource reporting.

---

## TTFT Predictor

`instance/TTFT_predictor/` contains an experimental TTFT / prefill prediction subsystem.

Main files:

| File | Purpose |
|---|---|
| `prefill_regressor.py` | Collects warmup samples and fits the regression model. |
| `prefill_predictor.py` | Provides async prediction and online update helpers. |
| `prefill_prediction_server.py` | Exposes the predictor as a FastAPI service. |
| `request_generator.py` | Generates prompts of target token lengths for warmup. |
| `WORKFLOW.md` | Describes predictor call paths and warmup workflow. |

The simplified model is based on features such as:

```text
batch_size * prompt_length
prompt_length
batch_size
```

This predictor is separate from the Resource Agent path. The Resource Agent observes host/device state; the TTFT predictor estimates model prefill latency.

---

## End-to-End Resource Monitor Smoke Test

Run:

```bash
python3 test/demo_resource_monitor_e2e.py \
  --agent-listen 127.0.0.1:19201 \
  --agent-url http://127.0.0.1:19201
```

The script starts `demo_proxy.py` and `demo_instance.py`, waits until the Proxy observes resource reports, terminates the Instance, and checks that the demo-owned Resource Agent is no longer reachable.

Use a non-default agent port to avoid accidentally reusing an already running agent on `9201`.

---

## Troubleshooting

### Proxy shows `unknown_instance`

Start the Proxy before the Instance:

```bash
python3 test/demo_proxy.py --host 127.0.0.1 --port 8001
python3 test/demo_instance.py --host 127.0.0.1 --port 9001 --proxy-cp-url http://127.0.0.1:8002
```

Also check for stale demo processes or containers that still send heartbeat messages with old `INSTANCE_ID` values.

### `cargo: command not found`

Install Rust/Cargo or use the CacheRoute development image that includes Rust.

### Resource Agent fails with Cargo lockfile errors

Use a recent stable Rust toolchain:

```bash
rustup update stable
rustup default stable
```

Avoid mixing root-owned build artifacts and non-root cargo commands. If needed, remove the Rust build directory:

```bash
rm -rf instance/resource_agent/target
```

### Port `9201` is already in use

Use another agent port:

```bash
python3 test/demo_instance.py \
  --resource-agent-listen 127.0.0.1:19201 \
  --resource-agent-url http://127.0.0.1:19201
```

### GPU list is empty

Check whether the process can run:

```bash
nvidia-smi
```

Inside Docker, make sure the container was started with GPU access, for example `--gpus all`.

---

## Current Status

Completed:

- Instance service-plane adapter for Proxy-forwarded OpenAI-compatible requests.
- Instance control-plane registration and heartbeat with Proxy.
- KVCache injection control-plane path.
- Demo-managed Rust Resource Agent startup and cleanup.
- Resource snapshot reporting to Proxy after registration.
- Proxy-side resource inspection APIs.
- Local dashboard tools for resource snapshots.
- Experimental TTFT prediction subsystem.

Not yet implemented:

- resource-aware Instance selection strategy in Proxy;
- detailed vLLM runtime queue metrics;
- detailed KVCache block residency metrics;
- lower-overhead GPU collection through NVML or a similar API.
