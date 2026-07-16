# Proxy

The Proxy is the local scheduling and knowledge-injection component in CacheRoute. It receives requests forwarded by the Scheduler, selects a local Instance, prepares text or KVCache injection, and forwards the request to the selected Instance.

In the two-level CacheRoute scheduling pipeline, the Scheduler decides **which LLM system / Proxy** should receive a request, while the Proxy decides **which local Instance** should execute it and **how knowledge injection** should be performed.

```text
Client
  └──> Scheduler
        └──> Proxy
              ├── maintains a local Instance pool
              ├── receives Instance resource snapshots
              ├── selects a local Instance
              ├── prepares text or KVCache injection
              └── forwards the request to Instance / vLLM + LMCache
```

## Runtime planes

| Plane | Default port | Description |
|---|---:|---|
| Service plane | `8001` | Receives Scheduler-forwarded OpenAI-compatible requests. |
| Control plane | `8002` | Receives Instance registration, heartbeat, topology reports, resource snapshots, and debug queries. |
| Browser UI | `8202` | Displays Proxy health, Instance liveness, resources, topology, Scheduler registration, charts, filters, and raw diagnostics. |

The Proxy can run without a Scheduler during local demos. In that case, Scheduler registration may fail non-fatally, but Instance registration and resource reporting can still be validated through the Proxy control plane and Proxy UI.

## Directory structure

```text
proxy/
├── proxy.py                     # Proxy service plane and startup lifecycle
├── proxy_cli.py                 # Proxy CLI for status inspection
├── sclient/
│   └── scheduler_client.py      # Proxy -> Scheduler control-plane client
├── resource/
│   ├── instance_pool.py         # Instance pool and normalized resource state
│   ├── p_control_plane.py       # Proxy control plane
│   └── hb_log.py                # Heartbeat reporting
├── strategy/
│   ├── base.py                  # Base Instance selection strategy
│   ├── round_robin.py           # Round-robin Instance selection
│   ├── least_load.py            # Experimental least-load Instance selection
│   ├── least_inflight.py        # Reserved for future strategy extension
│   └── factory.py               # Strategy builder
├── queue/
│   ├── manager.py               # Prepare/ready queue manager
│   ├── task.py                  # ProxyTask state
│   ├── instance_queues.py       # Per-Instance queues
│   └── knowledge.py             # Knowledge retrieval and injection helpers
└── README.md
```

The browser UI implementation lives outside this directory under [`UI/proxy_ui/`](../UI/proxy_ui/).

## Quick start

Start Proxy from the `test` directory:

```bash
cd test
python3 demo_proxy.py \
  --host 127.0.0.1 \
  --port 8001 \
  --strategy round_robin \
  --injection-strategy iws
```

`demo_proxy.py` starts the Proxy browser UI by default and prints:

```text
[demo_proxy] Proxy UI available at: http://127.0.0.1:8202
```

Open:

```text
http://127.0.0.1:8202
```

Common options:

| Option | Description |
|---|---|
| `--host` | Proxy service-plane bind host. The demo also uses it as the advertised host. |
| `--port` | Proxy service-plane bind port. The demo also uses it as the advertised port. |
| `--strategy` | Local Instance selection strategy. Defaults to `round_robin`; use `least_load` for the experimental load-aware selector. |
| `--injection-strategy` | Knowledge injection strategy. Use `default` or `iws`. |
| `--ready-release-policy` | Ready queue release policy: `ordered` or `text_bypass`. |
| `--kdn-links-json` | Optional static KDN topology metadata. |
| `--proxy-ui` | Explicitly enable the browser Proxy UI. This is already the default. |
| `--no-proxy-ui` | Disable the browser Proxy UI subprocess. |
| `--proxy-ui-listen HOST:PORT` | UI server listen address, default `127.0.0.1:8202`. |
| `--proxy-ui-url URL` | Browser-facing URL printed in logs, useful for tunnels / forwarded ports. |

## Proxy browser UI

The Proxy UI is the main browser observability dashboard for the Proxy runtime. It is frontend-only and does not mutate Scheduler routing, Proxy Instance selection, injection strategy, KDN behavior, KVCache behavior, or Instance forwarding.

Default URL:

```text
http://127.0.0.1:8202
```

It shows:

- Proxy health and `/debug/status`.
- Scheduler registration state, best effort.
- Instance pool and TTL-derived alive / stale state.
- Instance resource snapshots reported by demo Instances.
- Per-Instance resource cards and sortable tables.
- CPU, memory, GPU, network, and alive/stale trend charts.
- KDN topology links.
- Raw diagnostic JSON with copy actions.
- Local controls for refresh, pause/resume, polling interval, filters, search, sort, chart history, and theme.

The UI server proxies requests through `UI/proxy_ui/proxy_ui_server.py` so the browser does not need direct CORS access to Proxy or Scheduler APIs.

See [`UI/proxy_ui/README.md`](../UI/proxy_ui/README.md) for details.

## Startup lifecycle

```text
Proxy startup
  ├── initialize InstancePool
  ├── start Proxy control plane on :8002
  ├── start Proxy browser UI on :8202 in demo mode, unless disabled
  ├── load Instance selection strategy
  ├── register to Scheduler control plane, non-fatal for local demos
  ├── report topology metadata to Scheduler, if configured
  └── start heartbeat loop
```

During shutdown, the Proxy tries to unregister from the Scheduler. If the process is killed directly, Scheduler removes it after heartbeat expiry. In demo mode, the UI subprocess started by `demo_proxy.py` is cleaned up on exit.

## Control-plane APIs

### Health

```text
GET /healthz
GET /debug/status
```

### Instance management

```text
POST /v1/instance/register
POST /v1/instance/heartbeat
POST /v1/instance/unregister
GET  /v1/instance/list?include_dead=true
```

Instances register static information such as `instance_id`, `host`, `port`, `endpoints`, `tags`, `weight`, and `meta`. Heartbeats refresh `last_seen_at` and can optionally report lightweight load fields such as `inflight`, `qps_1m`, and `gpu_util`.

### Instance resource snapshots

After PR #87, demo Instances can report host resource snapshots to the Proxy control plane:

```text
POST /v1/instance/resource_snapshot
GET  /debug/instance_resources
```

The reporting path is:

```text
test/demo_instance.py
  ├── starts or reuses Rust Resource Agent
  ├── waits for Resource Agent /healthz
  ├── starts reporting only after Instance registration succeeds
  └── POSTs snapshots to Proxy /v1/instance/resource_snapshot
```

Successful snapshot updates are no longer logged on every report at `INFO`. The Proxy logs the first successful snapshot per Instance and uses debug-level logging for repeated successful updates.

The resource state appears in both APIs:

```bash
curl -sS "http://127.0.0.1:8002/debug/instance_resources" | python3 -m json.tool
curl -sS "http://127.0.0.1:8002/v1/instance/list?include_dead=true" | python3 -m json.tool
```

Normalized resource fields include:

| Field | Meaning |
|---|---|
| `cpu_util` | CPU utilization percentage from the agent snapshot. |
| `memory_used_mb` / `memory_total_mb` / `memory_free_mb` | Host memory snapshot. |
| `memory_free_ratio` | Admission-oriented memory free ratio. |
| `gpu_util_avg` | Average GPU utilization if GPUs are visible. |
| `gpu_mem_used_mb` / `gpu_mem_total_mb` | Aggregated GPU memory. |
| `network_rx_mbps` / `network_tx_mbps` | First observed network interface throughput. |
| `admission_state` | Agent capacity hint, such as `accepting`, `degraded`, or `rejecting`. |
| `resource_ts_ms` | Agent collection timestamp. |
| `resource_reported_at` | Proxy receive time in seconds. |
| `resource_report_monotonic_ms` | Reporter monotonic timestamp. |
| `resource_report_wall_time_ms` | Reporter wall-clock timestamp. |
| `reported_instance_id` | Instance ID carried by the report metadata. |
| `raw_resource` | Raw agent snapshot retained for debugging. |

Resource snapshots are currently **observability data**. They are not yet used by the active Instance selection strategy.

### Topology reporting

```text
POST /v1/topology/report
GET  /v1/topology/kdn_links
```

Instances can report measured KDN link metrics. When multiple Instances report the same KDN, the Proxy keeps the best link using higher bandwidth and lower latency as the preference rule. The merged topology can later be reported to the Scheduler.

## Request workflow

A Scheduler-forwarded request is processed as follows:

```text
Scheduler
  └──> Proxy service plane :8001
        ├── recover CacheRoute Request
        ├── build OpenAI-compatible Instance request body
        ├── select a local Instance
        ├── optionally run IWS injection decision
        ├── create ProxyTask
        ├── enqueue task into prepare queue
        ├── prepare text or KVCache injection
        ├── move task into ready queue
        ├── forward request to Instance
        └── return Instance response to Scheduler
```

Supported service-plane endpoints:

```text
POST /v1/chat/completions
POST /v1/completions
```

For streaming chat completion, the Proxy forwards the downstream SSE stream and appends a `cacheroute_meta` SSE event before `[DONE]`. This metadata contains CacheRoute timing and injection traces.

## Instance selection

### Proxy Instance selection strategies

| Strategy | Aliases | Status | Description |
|---|---|---|---|
| `round_robin` | `round_robin`, `round-robin`, `rr` | Default | Selects alive Instances in round-robin order. |
| `least_load` | `least_load`, `least-load`, `ll` | Experimental | Selects the lowest known Instance load by `load.inflight`, then uses `qps_1m` as a secondary signal. Missing metrics remain unknown rather than zero; if all candidates lack usable load metrics, selection falls back to round-robin. |
| `kv_aware` | Planned | Planned | Future strategy intended to consider KVCache locality/inventory together with runtime load. |

Select `least_load` from the demo CLI:

```bash
python3 test/demo_proxy.py --strategy least_load
```

Or select it through the environment:

```bash
PROXY_INSTANCE_STRATEGY=least_load python3 test/demo_proxy.py
```

If no alive Instance is available, the Proxy returns:

```text
503 no_instance
```

The strategy interface is extensible. Future policies can use queue state, resource snapshots, prefix locality, KVCache inventory, or KDN topology.

## Injection strategies

| Mode | Description |
|---|---|
| `default` | Uses the injection mode carried by the Scheduler request. |
| `iws` | Dynamically selects text injection or KVCache injection based on predicted cost. |

The IWS mode estimates text-injection and KVCache-injection costs, then chooses KVCache only when the expected benefit is large enough.

Simplified cost shape:

```text
text_total
  = max(text_prepare_wait, ready_wait)
    + text_prefill_service

kvcache_total
  = max(kvcache_prepare, ready_wait)
    + redis_load
    + residual_prefill

choose KVCache if:
  kvcache_total + kdn_queue_penalty + decision_margin < text_total
```

This lets the Proxy avoid KVCache when the KDN path is congested and use KVCache when transfer latency can be hidden or prefill savings are large.

## Prepare and ready queues

The Proxy uses two stages for each Instance.

### Prepare queue

The prepare queue handles knowledge preparation:

- fetch knowledge from KDN;
- classify knowledge into `kv_ready`, `text_only`, and `miss`;
- inject retrieved text into the prompt;
- trigger KVCache injection through Instance control plane;
- collect timing traces.

Concurrency is controlled by:

```text
PREPARE_CONCURRENCY
```

### Ready queue

The ready queue controls when prepared tasks are forwarded to the selected Instance. It maintains a predicted execution timeline with slot readiness, prefill start, first token, decode tail estimate, predicted queue wait, and predicted TTFT.

Concurrency is controlled by:

```text
READY_CONCURRENCY
```

## Ready release policy

| Policy | Description |
|---|---|
| `ordered` | Release tasks in prepare sequence order. |
| `text_bypass` | Allow text tasks to bypass blocked KVCache tasks within a configured limit. |

Relevant variables:

```text
PROXY_READY_RELEASE_POLICY
PROXY_TEXT_BYPASS_MAX_PER_FLUSH
```

## KVCache injection path

```text
Proxy
  ├── fetch knowledge metadata from KDN
  ├── identify kv-ready knowledge IDs
  ├── estimate KDN-to-Instance KV transfer time
  ├── reserve KDN KV link
  ├── call Instance control plane POST /v1/kv/inject_ready
  ├── wait for KV injection acknowledgement
  └── forward request to Instance
```

If KVCache injection fails or no KV-ready knowledge exists, the Proxy falls back to text-only behavior and records the fallback path in the task trace.

## Proxy CLI

```bash
python3 proxy/proxy_cli.py
```

Useful commands:

| Command | Description |
|---|---|
| `:status` | Show Proxy control-plane health and Instance counts. |
| `:instances [N]` | List alive Instances. |
| `:instances --all [N]` | List all Instances, including expired ones. |
| `:watch [--all] [--interval S] [--limit N]` | Continuously refresh Proxy status. |
| `:scheduler` | Query Scheduler control plane. |
| `:exit` / `:quit` | Exit. |

The browser Proxy UI covers the same observability surface and adds charts, cards, filters, and raw diagnostic copy actions.

## Validation

### 1. Start Proxy

```bash
cd test
python3 demo_proxy.py \
  --host 127.0.0.1 \
  --port 8001 \
  --strategy round_robin \
  --injection-strategy iws
```

Open the Proxy UI printed by the demo:

```text
http://127.0.0.1:8202
```

### 2. Start Instance with default demo resource monitoring

```bash
cd test
python3 demo_instance.py \
  --host 127.0.0.1 \
  --port 9001 \
  --proxy-cp-url http://127.0.0.1:8002
```

### 3. Inspect resource state

```bash
curl -sS "http://127.0.0.1:8002/debug/instance_resources" | python3 -m json.tool
```

Or use the browser UI:

```text
http://127.0.0.1:8202
```

### 4. Run the e2e smoke validation

```bash
python3 test/demo_resource_monitor_e2e.py \
  --agent-listen 127.0.0.1:19201 \
  --agent-url http://127.0.0.1:19201
```

Use a non-default agent port if another Resource Agent is already running.

## Runtime options

Common environment variables:

| Variable | Description |
|---|---|
| `PROXY_ID` | Proxy ID reported to Scheduler. |
| `PROXY_ADVERTISE_HOST` | Host reported to Scheduler. |
| `PROXY_ADVERTISE_PORT` | Service-plane port reported to Scheduler. |
| `PROXY_CP_HOST` / `PROXY_CP_PORT` | Proxy control-plane bind address. |
| `PROXY_INSTANCE_STRATEGY` | Local Instance selection strategy. |
| `PROXY_INJECTION_STRATEGY` | Injection strategy, `default` or `iws`. |
| `PROXY_READY_RELEASE_POLICY` | Ready release policy. |
| `PROXY_KDN_LINKS_JSON` | Static KDN topology metadata. |
| `PROXY_INSTANCE_TTL_S` | Instance alive TTL. |
| `PREPARE_CONCURRENCY` | Prepare queue concurrency. |
| `READY_CONCURRENCY` | Ready worker concurrency. |
| `PROXY_UI_LISTEN` | Optional default for the Proxy UI listen address. |
| `PROXY_UI_URL` | Optional browser-facing URL printed by `demo_proxy.py`. |

## Notes

- Resource snapshots are visible in Proxy state but do not yet drive routing.
- Repeated successful resource reports are intentionally quiet to avoid log flooding.
- `unknown_instance` warnings usually mean a stale or external Instance process is still heartbeating to the Proxy control plane.
- The Proxy UI is the preferred visual entry point for Proxy observability during demos and experiments.
