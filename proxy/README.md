# Proxy

The Proxy is the local scheduling and knowledge injection component in CacheRoute. It receives requests forwarded by the Scheduler, selects a local Instance, prepares knowledge injection, and forwards the request to the selected Instance.

In CacheRoute, the Proxy is the second stage of the two-level scheduling pipeline:

```text
Client
  └──> Scheduler
          └──> Proxy
                  ├── selects a local Instance
                  ├── chooses the knowledge injection mode
                  ├── prepares text or KVCache injection
                  └── forwards the request to Instance / vLLM + LMCache
```

The Scheduler decides **where a request should go** across LLM systems. The Proxy decides **how the request should be executed** inside the selected LLM system.

---

## Overview

The Proxy provides two planes:

| Plane | Default Port | Description |
|---|---:|---|
| Service plane | `8001` | Receives requests forwarded by the Scheduler and returns Instance responses. |
| Control plane | `8002` | Receives Instance registration, heartbeat, topology reports, and debugging queries. |

The Proxy maintains the following runtime state:

- **Instance pool:** alive Instances under this Proxy.
- **Topology metadata:** KDN-to-Instance or KDN-to-Proxy link information.
- **Per-instance queues:** prepare queues and ready queues for each Instance.
- **Injection state:** text injection, KVCache injection, and related timing traces.
- **Scheduler registration state:** Proxy registration and heartbeat with the Scheduler control plane.

---

## Directory Structure

```text
proxy/
├── proxy.py                     # Proxy service plane and startup lifecycle
├── proxy_cli.py                 # Proxy CLI for status inspection
├── sclient/
│   └── scheduler_client.py      # Proxy -> Scheduler control-plane client
├── resource/
│   ├── instance_pool.py         # Instance pool maintained by the Proxy
│   ├── p_control_plane.py       # Proxy control plane
│   └── hb_log.py                # Heartbeat reporting
├── strategy/
│   ├── base.py                  # Base Instance selection strategy
│   ├── round_robin.py           # Round-robin Instance selection
│   ├── least_inflight.py        # Reserved for future strategy extension
│   └── factory.py               # Strategy builder
├── queue/
│   ├── manager.py               # Prepare/ready queue manager
│   ├── task.py                  # ProxyTask state
│   ├── instance_queues.py       # Per-Instance queues
│   └── knowledge.py             # Knowledge retrieval and injection helpers
└── README.md
```

---

## Quick Start

Start the Proxy from the `test` directory:

```bash
cd test

python3 demo_proxy.py \
  --strategy round_robin \
  --injection-strategy iws \
  --ready-release-policy text_bypass \
  --kdn-links-json '{"kdn_local_1":{"bandwidth_tier":3,"latency_tier":1},"kdn_local_2":{"bandwidth_tier":1,"latency_tier":3}}'
```

Main options:

| Option | Description |
|---|---|
| `--strategy` | Local Instance selection strategy. Currently `round_robin` is supported. |
| `--injection-strategy` | Knowledge injection strategy. Use `default` or `iws`. |
| `--ready-release-policy` | Ready-queue release policy. Use `ordered` or `text_bypass`. |
| `--kdn-links-json` | Static KDN topology metadata reported to the Scheduler. |

Example for a minimal Proxy startup:

```bash
cd test
python3 demo_proxy.py --strategy round_robin
```

---

## Startup Lifecycle

When the Proxy starts, it performs the following steps:

```text
Proxy startup
  ├── initialize Instance pool
  ├── start Proxy control plane
  ├── load local Instance selection strategy
  ├── register itself to the Scheduler control plane
  ├── report topology metadata to the Scheduler
  └── start heartbeat loop
```

The Proxy can still run locally if Scheduler registration fails, but the Scheduler will not be able to route requests to it until registration succeeds.

During shutdown, the Proxy tries to unregister itself from the Scheduler. If the process is killed directly, the Scheduler removes it after its heartbeat expires.

---

## Control Plane

The Proxy control plane maintains local Instance state and topology information.

### Instance management

```text
POST /v1/instance/register       Register an Instance under this Proxy.
POST /v1/instance/heartbeat      Update Instance runtime load.
POST /v1/instance/unregister     Remove an Instance from the pool.
GET  /v1/instance/list           List alive or all Instances.
```

Instance heartbeats can report:

```text
inflight
qps_1m
gpu_util
```

The Proxy uses this information to maintain the local Instance pool.

### Topology reporting

```text
POST /v1/topology/report         Report KDN link metrics from an Instance.
GET  /v1/topology/kdn_links      Show the merged KDN link snapshot.
```

When multiple Instances report metrics for the same KDN, the Proxy keeps the best link according to higher bandwidth and lower latency.

The resulting topology metadata is reported to the Scheduler through Proxy heartbeat. The Scheduler can then use it for topology-aware Proxy selection.

---

## Request Workflow

A request forwarded by the Scheduler is processed as follows:

```text
Scheduler
  └──> Proxy service plane :8001
          ├── recover Request from Scheduler payload
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

The Proxy supports both OpenAI-compatible endpoints:

```text
POST /v1/chat/completions
POST /v1/completions
```

For chat completion, the Proxy streams the downstream response back to the Scheduler. It also appends a `cacheroute_meta` SSE event before `[DONE]`, which contains CacheRoute traces such as KVCache injection status and knowledge classification results.

---

## Instance Selection

The Proxy selects a local Instance before preparing the request.

Current active strategy:

| Strategy | Description |
|---|---|
| `round_robin` | Selects alive Instances in round-robin order. |
| `prefix-aware` | To be update. |

The strategy reads alive Instances from the Instance pool. If no alive Instance is available, the Proxy returns:

```text
503 no_instance
```

The strategy interface is extensible. Additional policies, such as least-inflight or latency-aware selection, can be implemented under `proxy/strategy/`.

---

## Injection Strategy

The Proxy supports two injection strategy modes.

| Mode | Description |
|---|---|
| `default` | Uses the injection mode carried by the Scheduler request. |
| `iws` | Dynamically selects text injection or KVCache injection based on predicted cost. |

### IWS: Injection Willingness Strategy

The IWS mode estimates the cost of text-based injection and KVCache-based injection, then applies the lower-cost mode when the margin is large enough.

The decision considers:

- current ready-queue wait time;
- text preparation cost;
- text prefill cost;
- KVCache transfer cost;
- KDN link queueing delay;
- Redis KV loading cost;
- residual prefill cost after KVCache reuse.

The simplified decision logic is:

```text
text_total
  = max(text_prepare_wait, ready_wait)
    + text_prefill_service

kvcache_total
  = max(kvcache_prepare, ready_wait)
    + redis_load
    + residual_prefill

kvcache_score
  = kvcache_total
    + kdn_queue_penalty

choose KVCache if:
  kvcache_score + decision_margin < text_total
otherwise:
  choose text injection
```

This allows the Proxy to avoid KVCache injection when the KDN path is congested, and to use KVCache injection when the transfer cost can be hidden by queue waiting or when compute savings are large enough.

---

## Prepare and Ready Queues

The Proxy uses a two-stage queue design for each Instance.

```text
ProxyTask
  └──> prepare queue
          ├── fetch knowledge metadata from KDN
          ├── classify kv-ready / text-only / missing knowledge
          ├── build text context or trigger KVCache injection
          └── move task to ready queue
                  ├── reserve predicted prefill slot
                  ├── forward request to Instance
                  └── stream response back to Scheduler
```

### Prepare queue

The prepare queue handles knowledge preparation. It may:

- fetch knowledge from the selected KDN;
- classify requested knowledge IDs into `kv_ready`, `text_only`, and `miss`;
- inject retrieved text into the prompt;
- trigger KVCache injection through the Instance control plane;
- collect timing traces for later analysis.

Prepare tasks can run concurrently, controlled by:

```text
PREPARE_CONCURRENCY
```

### Ready queue

The ready queue controls when prepared tasks are forwarded to the selected Instance.

It maintains a predicted execution timeline for each Instance, including:

- slot readiness;
- prefill start time;
- first-token time;
- decode tail estimate;
- predicted queue wait;
- predicted TTFT.

Ready workers forward requests to the Instance and record actual timing when the first token arrives.

Ready concurrency is controlled by:

```text
READY_CONCURRENCY
```

---

## Ready Release Policy

After preparation, tasks are released into the ready queue according to the ready release policy.

| Policy | Description |
|---|---|
| `ordered` | Release tasks in prepare sequence order. |
| `text_bypass` | Allow text-injection tasks to bypass blocked KVCache tasks, up to a configured limit. |

The `text_bypass` policy is useful when KVCache injection is delayed by KDN transfer or KV queueing, while later text-injection tasks are already prepared.

Related options:

```text
PROXY_READY_RELEASE_POLICY
PROXY_TEXT_BYPASS_MAX_PER_FLUSH
```

---

## KVCache Injection Path

When KVCache injection is selected, the Proxy follows this path:

```text
Proxy
  ├── fetch knowledge metadata from KDN
  ├── identify kv-ready knowledge IDs
  ├── estimate KDN-to-Instance KV transfer time
  ├── reserve the KDN KV link
  ├── call Instance control plane
  │     └── POST /v1/kv/inject_ready
  ├── wait for KV injection acknowledgement
  └── forward the request to Instance
```

The Instance control plane receives:

```text
request_id
kdn_addr
model
knowledge_ids
```

After KVCache injection succeeds, the Instance can reuse the injected KVCache through LMCache during inference.

If KVCache injection fails or no KV-ready knowledge exists, the Proxy falls back to text-only behavior and records the fallback path in the task trace.

---

## Topology Metadata

The Proxy can report KDN link information to the Scheduler. This helps the Scheduler choose a Proxy that has better network relation to the selected KDN.

Static topology can be provided at startup:

```bash
python3 demo_proxy.py \
  --strategy round_robin \
  --kdn-links-json '{"kdn_a":{"bandwidth_tier":3,"latency_tier":1}}'
```

The Proxy can also collect topology reports from Instances through:

```text
POST /v1/topology/report
```

A link item may contain fields such as:

```text
bandwidth_tier
latency_tier
bandwidth_mbps
latency_ms
rtt_ms
```

The Proxy merges these reports and sends the best KDN link snapshot to the Scheduler through heartbeat metadata.

---

## Proxy CLI

The Proxy provides a CLI for inspecting the control plane and the Scheduler registration state.

Start the CLI:

```bash
python3 proxy/proxy_cli.py
```

Optional arguments:

| Argument | Description |
|---|---|
| `--cp-url` | Proxy control plane URL. Default: `http://127.0.0.1:8002`. |
| `--scheduler-cp-url` | Scheduler control plane URL. Default: `http://127.0.0.1:7002`. |
| `--proxy-id` | Current Proxy ID. Default: read from `PROXY_ID`. |
| `--scheduler-proxy-list-path` | Scheduler Proxy list API path. Default: `/v1/proxy/list`. |
| `--timeout` | HTTP timeout in seconds. Default: `5`. |

REPL commands:

| Command | Description |
|---|---|
| `:help` | Show command help. |
| `:status` | Show Proxy control plane health and Instance counts. |
| `:instances [N]` | List alive Instances. Default: `N=20`. |
| `:instances --all [N]` | List all Instances, including expired ones. |
| `:watch [--all] [--interval S] [--limit N]` | Continuously refresh Proxy status. |
| `:scheduler` | Query Scheduler control plane and show whether this Proxy is registered. |
| `:exit` / `:quit` | Exit the CLI. |

---

## Runtime Options

Common environment variables:

| Variable | Description |
|---|---|
| `PROXY_ID` | Proxy ID reported to the Scheduler. |
| `PROXY_ADVERTISE_HOST` | Host reported to the Scheduler. |
| `PROXY_ADVERTISE_PORT` | Service-plane port reported to the Scheduler. |
| `PROXY_CP_HOST` | Proxy control-plane host. |
| `PROXY_CP_PORT` | Proxy control-plane port. |
| `PROXY_INSTANCE_STRATEGY` | Local Instance selection strategy. |
| `PROXY_INJECTION_STRATEGY` | Injection strategy, `default` or `iws`. |
| `PROXY_READY_RELEASE_POLICY` | Ready release policy, `ordered` or `text_bypass`. |
| `PROXY_KDN_LINKS_JSON` | Static KDN topology metadata. |
| `PROXY_INSTANCE_TTL_S` | Instance alive TTL. |
| `PREPARE_CONCURRENCY` | Per-Instance prepare concurrency. |
| `READY_CONCURRENCY` | Per-Instance ready worker concurrency. |
| `IWS_KDN_QUEUE_PENALTY_ALPHA` | Penalty weight for KDN queueing in IWS. |
| `IWS_DECISION_MARGIN_MS` | Decision margin used by IWS. |
| `KDN_DEFAULT_BANDWIDTH_MBPS` | Default bandwidth used when no topology bandwidth is available. |

---

## Validation

### 1. Start Scheduler

```bash
cd test
python3 demo_scheduler.py --cacheroute
```

### 2. Start Proxy

```bash
cd test

python3 demo_proxy.py \
  --strategy round_robin \
  --injection-strategy iws \
  --ready-release-policy text_bypass
```

### 3. Start Instance

```bash
cd test
python3 demo_instance.py --port 9001 --host 127.0.0.1
```

### 4. Check Proxy control plane

```bash
curl -s http://127.0.0.1:8002/healthz | python3 -m json.tool
```

List alive Instances:

```bash
curl -s http://127.0.0.1:8002/v1/instance/list | python3 -m json.tool
```

### 5. Check whether Proxy is registered in Scheduler

```bash
python3 proxy/proxy_cli.py
```

Then run:

```text
:scheduler
```

### 6. Send a request through Scheduler

```bash
python3 test/demo_client.py --with-ui
```

or send an OpenAI-compatible request to the Scheduler service plane.

---

## Runtime Screenshots

### Proxy startup

<img width="1200" height="125" alt="Proxy startup" src="https://github.com/user-attachments/assets/07b78380-bd7d-47ae-8f7d-f45cdd7882cb" />

### CLI commands

<img width="1200" height="418" alt="Proxy CLI commands" src="https://github.com/user-attachments/assets/0e161d8a-1321-436c-a78d-81feae125987" />

### Proxy registration on Scheduler

<img width="1200" height="184" alt="Proxy Scheduler status" src="https://github.com/user-attachments/assets/192ae569-d0ac-419c-b84f-db1c2a7a0f31" />

### Instance pool

<img width="1200" height="144" alt="Proxy Instance pool" src="https://github.com/user-attachments/assets/183ccc5b-65dc-426d-843a-c8c1509fb7ab" />

### Queue and injection timing

<img width="1200" height="1319" alt="Proxy queue timing" src="https://github.com/user-attachments/assets/afc7a7e5-bf38-4520-9b4a-8b354d1ee089" />

<img width="1200" height="1340" alt="Proxy injection timing" src="https://github.com/user-attachments/assets/35429add-d4d7-431d-bca4-344c67c6f966" />

---

## Notes

- The Proxy is designed for local scheduling inside one LLM system.
- The Scheduler performs global routing, while the Proxy performs local Instance selection and injection strategy selection.
- The current active Instance strategy is `round_robin`. Other strategies can be added under `proxy/strategy/`.
- The `iws` injection strategy is experimental and is used to validate compute-network-aware knowledge injection.
- KVCache injection requires a running KDN Server, Instance control plane, and LMCache-compatible KV backend.
- The default examples use a single-machine setup. For multi-machine deployment, update service addresses in `core/config.py`.
