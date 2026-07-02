# Scheduler

The Scheduler is the global request routing component in CacheRoute. It receives OpenAI-compatible inference requests, maintains the runtime state of KDN servers and Proxy nodes, and routes each request to a suitable LLM system according to the selected scheduling strategy.

In CacheRoute, the Scheduler is the first stage of the two-level scheduling pipeline:

```text
Client
  └──> Scheduler (first stage scheduling)
          ├── selects a KDN server
          └── selects a Proxy / LLM system
                  └──> Proxy (second stage scheduling)
                          └──> Instance / vLLM + LMCache
```

The Scheduler focuses on **knowledge-oriented task routing**. It considers knowledge availability, KDN load, topology information, Proxy load, and knowledge affinity before forwarding a request to the target Proxy.

---

## Overview

The Scheduler provides two planes:

| Plane | Default Port | Description |
|---|---:|---|
| Service plane | `7001` | Receives OpenAI-compatible inference requests and forwards them to selected Proxies. |
| Control plane | `7002` | Receives registration, heartbeat, and runtime updates from KDN servers and Proxies. |

The Scheduler maintains two runtime resource pools:

- **KDN pool:** tracks available KDN servers, registered knowledge items, KVCache availability, and KDN runtime load.
- **Proxy pool:** tracks available LLM systems, topology information, inflight load, recent QPS, and GPU utilization.

These resource pools are used by the CacheRoute strategy to make routing decisions.

---

## Quick Start

Start the Scheduler from the `test` directory:

```bash
cd CacheRoute/test
python3 demo_scheduler.py --strategy <strategy_name>
```

For CacheRoute routing:

```bash
cd CacheRoute/test
python3 demo_scheduler.py --cacheroute
```

The Scheduler needs model-related configuration to analyze request knowledge requirements. Check the following settings in `core/config.py`:

```text
SCHEDULER_MODEL_PATH        Path of the model used by the Scheduler.
SCHEDULER_TOKENIZER_MAP     Tokenizer path or tokenizer mapping.
SCHEDULER_EMBEDDING_MODEL   Embedding model path.
```

---

## Scheduler CLI

The Scheduler provides a CLI tool for inspecting and debugging the runtime resource pools.

Start the CLI:

```bash
cd scheduler
python3 scheduler_cli.py
```

The CLI supports viewing knowledge status, KDN pool status, Proxy pool status, and strategy information.

<img width="800" alt="Scheduler CLI" src="https://github.com/user-attachments/assets/a63ef61f-b3e6-40e8-b132-6c978dd43f25" />

### Knowledge status

<img  width="800" alt="Knowledge status" src="https://github.com/user-attachments/assets/7f1e1d81-c599-4ae2-9e76-83f66030d4fa" />

### KDN pool status

<img  width="800" alt="KDN pool status" src="https://github.com/user-attachments/assets/d02024c0-64ae-4c8b-a639-772003247b3f" />

### Proxy pool status

<img width="800" alt="Proxy pool status" src="https://github.com/user-attachments/assets/3ecdcc5f-f238-4443-888a-b8635811254a" />

### Strategy information

<img  width="800" alt="Scheduler strategy information" src="https://github.com/user-attachments/assets/508b5447-ce8b-49fc-b530-2e176868965b" />

---

## Scheduling Workflow

A request is processed by the Scheduler as follows:

```text
User Request
  └──> Scheduler service plane :7001
          ├── Request.build_request()
          │     └── parse request and extract knowledge requirements
          ├── KDN pool
          │     └── check knowledge availability and KDN runtime state
          ├── Proxy pool
          │     └── check topology, load, and knowledge affinity
          └── forward_request()
                └── forward the request to the selected Proxy
```

`Request.build_request()` is the point where the incoming request is parsed and prepared for scheduling. The selected strategy then uses the KDN pool and Proxy pool to determine the routing result.

---

## CacheRoute Strategy

The `cacheroute` strategy performs knowledge-oriented routing in two stages.

### Stage 1: KDN Selection

The Scheduler first selects a KDN server according to knowledge availability and KDN runtime state.

Current selection order:

```text
text_full
  -> not_overloaded
  -> kv_cover_len
  -> load / tie-break
```

The main idea is:

1. prefer KDN servers that contain the required text knowledge;
2. filter out overloaded KDN servers;
3. prefer KDN servers with better KVCache coverage;
4. use runtime load and tie-breaking rules when multiple candidates remain.

### Stage 2: Proxy Selection

After selecting a KDN server, the Scheduler selects a target Proxy / LLM system.

Current selection order:

```text
topology_best_group
  -> load_safe_window
  -> knowledge_affinity
  -> load / tie-break
```

The main idea is:

1. prefer Proxies with better topology relation to the selected KDN;
2. filter Proxies by the load safety window;
3. prefer Proxies with recent knowledge affinity;
4. use runtime load and tie-breaking rules when multiple candidates remain.

### Debug Interfaces

The Scheduler exposes two debug APIs:

```text
GET /debug/status
GET /debug/strategy
```

`/debug/status` shows current resource pools and runtime state.

`/debug/strategy` shows recent strategy decisions, including KDN candidates, Proxy candidates, selected nodes, and strategy counters.

---

## Runtime Options

The following options are useful for validating and tuning CacheRoute routing.

| Option | Description |
|---|---|
| `--kdn-pending-overload-th <int>` | Marks a KDN as overloaded when pending transfers exceed the threshold. |
| `--kdn-active-overload-th <int>` | Marks a KDN as overloaded when active transfers exceed the threshold. |
| `--kdn-queue-ms-overload-th <float>` | Marks a KDN as overloaded when the estimated queue delay exceeds the threshold. |
| `--proxy-load-ratio-delta <float>` | Sets the safe load window used for Proxy selection. |
| `--cacheroute-log-decision {0/1}` | Prints one-line routing logs for each request. |

These options can be configured in two ways:

1. pass command-line arguments to the demo script;
2. set default values in `core/config.py`.

Example:

```bash
python3 test/demo_scheduler.py \
  --cacheroute \
  --kdn-pending-overload-th 8 \
  --kdn-active-overload-th 4 \
  --kdn-queue-ms-overload-th 30 \
  --cacheroute-log-decision 1
```

---

## Strategy Validation

### 1. Start the Scheduler

```bash
cd test
python3 demo_scheduler.py --cacheroute
```

### 2. Start a Proxy with topology information

Topology information is optional, but recommended when validating the second-stage Proxy selection.

```bash
python3 demo_proxy.py \
  --strategy least_inflight \
  --kdn-links-json '{"kdn_a":{"bandwidth_tier":3,"latency_tier":1}}'
```

### 3. Start a KDN with network load reporting (for single machine evaluations)

To let the KDN report runtime load such as pending transfers, active transfers, and queue delay, enable the KDN network simulator.

```bash
python3 demo_kdn.py \
  --network \
  --network-bw-mb-s 125 \
  --network-batch-window-ms 10 \
  --network-fixed-latency-ms 10 \
  --network-efficiency 0.8
```

### 4. Check Scheduler status

```bash
curl -s http://127.0.0.1:7001/debug/status | python3 -m json.tool
```

Important fields:

| Field | Description |
|---|---|
| `strategy` | Should be `cacheroute`. |
| `proxies` | Shows Proxy runtime state, such as `inflight`, `qps_1m`, and `gpu_util`. |
| `kdns` | Shows KDN runtime state, such as `items`, `pending_transfers`, `active_transfers`, and `network_queue_ms_ema`. |
| `kdn_alive` | Shows whether KDN servers are alive. |
| `kdn_alive_addrs` | Shows alive KDN addresses. |

### 5. Check recent strategy decisions

```bash
curl -s http://127.0.0.1:7001/debug/strategy | python3 -m json.tool
```

Important fields:

| Field | Description |
|---|---|
| `strategy` | Current scheduling strategy. |
| `strategy_debug.kdn_candidates` | KDN candidates considered by the strategy. |
| `strategy_debug.proxy_candidates` | Proxy candidates considered by the strategy. |
| `strategy_debug.chosen_kdn_id` | Selected KDN server. |
| `strategy_debug.chosen_proxy_id` | Selected Proxy. |
| `strategy_debug.counters` | Strategy counters, such as request count, topology hit count, and load filtering count. |

### 6. Observe one-line routing logs

By default, CacheRoute prints one-line routing logs:

```text
[CacheRoute] req=... kdn=... proxy=... kids=...
```

To disable this log:

```bash
export SCHEDULER_CACHEROUTE_LOG_DECISION=0
```

---

## Resource Pool Maintenance

The Scheduler control plane maintains KDN and Proxy resource pools through registration and heartbeat messages.

```text
Scheduler control plane :7002
  ├── register
  ├── heartbeat
  └── unregister
```

### Proxy Pool

The Proxy pool maintains both static and dynamic information:

- service address;
- topology relation to KDN servers;
- inflight requests;
- recent QPS;
- GPU utilization;
- recent knowledge history.

### KDN Pool

The KDN pool maintains:

- KDN service address;
- alive status;
- available knowledge items;
- KVCache availability summary;
- recent QPS;
- pending transfer count;
- active transfer count;
- network queue delay estimate.

During scheduling, the Scheduler reads the current alive state and runtime summaries from the resource pools. It does not rely on one-time request metadata only.

---

## Notes

- The current `cacheroute` strategy uses rule-based lexicographic filtering rather than weighted scoring.
- The Scheduler is designed for experimental validation of knowledge-oriented routing and compute-network-aware knowledge injection.
- The default demo uses loopback addresses. For multi-machine deployment, update the addresses in `core/config.py`.
- For end-to-end deployment with KDN, Proxy, Instance, vLLM, LMCache, and Redis, see the main `README.md`.
