# Core

The `core/` module contains shared configuration, request data structures, and forwarding utilities used by Scheduler, Proxy, Instance, and KDN Server.

It provides the common runtime interface that connects CacheRoute components together.

## Module overview

```text
core/
├── config.py              # Default configuration for all CacheRoute components
├── request.py             # Internal Request / Prompt / Service / Task data structures
├── fwd.py                 # HTTP forwarding utilities
├── model_calculation.py   # Model-side cost and size estimation helpers
└── README.md
```

Main responsibilities:

- define default service addresses and ports;
- define model, embedding, and knowledge retrieval configuration;
- define the internal request object passed from Scheduler to Proxy;
- normalize OpenAI-compatible requests into CacheRoute request metadata;
- provide common forwarding utilities for component communication.

## Configuration

Most default runtime parameters are centralized in:

```text
core/config.py
```

The configuration is grouped by component:

| Section | Description |
|---|---|
| Client | Required and optional OpenAI-compatible request fields. |
| Scheduler | Scheduler service plane, control plane, strategy, retrieval, and heartbeat settings. |
| Proxy | Proxy service plane, control plane, Instance pool, queue, and topology settings. |
| Instance | Instance service plane, control plane, vLLM URL, Redis, KDN topology, and resource monitoring settings. |
| KDN Server | KDN service address, network simulation, Redis rewrite, and KVCache build defaults. |
| Other | Legacy or reserved configuration fields. |

The default configuration uses loopback addresses for single-machine demos.

```text
Scheduler service plane:  http://127.0.0.1:7001
Scheduler control plane:  http://127.0.0.1:7002
Proxy service plane:      http://127.0.0.1:8001
Proxy control plane:      http://127.0.0.1:8002
Instance service plane:   http://127.0.0.1:9001
Instance control plane:   http://127.0.0.1:9002
Resource Agent:           http://127.0.0.1:9201
KDN Server:               http://127.0.0.1:9101
vLLM backend:             http://127.0.0.1:8000
Redis:                    127.0.0.1:6379
```

## Instance resource monitoring configuration

The demo resource monitor defaults live in the Instance section of `core/config.py`.

| Variable | Default | Description |
|---|---:|---|
| `INSTANCE_RESOURCE_MONITOR_ENABLE` | `True` | Enable demo resource monitoring by default in `test/demo_instance.py`. |
| `INSTANCE_RESOURCE_AUTO_START_AGENT` | `True` | Auto-start the Rust Resource Agent when it is not reachable. |
| `INSTANCE_RESOURCE_AGENT_HOST` | `127.0.0.1` | Agent host for local demos. |
| `INSTANCE_RESOURCE_AGENT_PORT` | `9201` | Agent port. |
| `INSTANCE_RESOURCE_AGENT_LISTEN` | `127.0.0.1:9201` | Agent listen address passed to the Rust binary. |
| `INSTANCE_RESOURCE_AGENT_URL` | `http://127.0.0.1:9201` | Agent base URL used by the reporter. |
| `INSTANCE_RESOURCE_AGENT_SAMPLE_INTERVAL_MS` | `1000` | Agent sampling interval. |
| `INSTANCE_RESOURCE_AGENT_START_TIMEOUT_S` | `60.0` | Max wait time for agent readiness. |
| `INSTANCE_RESOURCE_REPORT_ENABLE` | `False` | Base default. `demo_instance.py` enables reporting when monitoring is enabled unless `--no-resource-report` is passed. |
| `INSTANCE_RESOURCE_REPORT_HZ` | `1.0` | Default report frequency. |
| `INSTANCE_RESOURCE_REPORT_INTERVAL_MS` | `1000` | Default report interval. Explicit CLI interval overrides Hz. |
| `INSTANCE_RESOURCE_REPORT_TIMEOUT_S` | `2.0` | HTTP timeout for snapshot/report calls. |

Common overrides:

```bash
export INSTANCE_RESOURCE_MONITOR_ENABLE=0
export INSTANCE_RESOURCE_AGENT_LISTEN=127.0.0.1:19201
export INSTANCE_RESOURCE_AGENT_URL=http://127.0.0.1:19201
export INSTANCE_RESOURCE_REPORT_INTERVAL_MS=500
```

`test/demo_instance.py` also exposes CLI flags for the same settings:

```bash
python3 test/demo_instance.py \
  --resource-agent-listen 127.0.0.1:19201 \
  --resource-agent-url http://127.0.0.1:19201 \
  --resource-report-interval-ms 500
```

## Request model

CacheRoute converts user requests into an internal `Request` object before scheduling.

```text
Request
├── Prompt
├── Service
└── Task
```

### Prompt

`Prompt` describes the user prompt and generation parameters.

| Field | Description |
|---|---|
| `model` | Model name used in the request. |
| `user_prompt` | User query extracted from `messages`, `prompt`, or `user_prompt`. |
| `token_length` | Estimated token length of the user prompt. |
| `bs` | Batch size. Currently defaults to `1`. |
| `max_tokens` | Maximum number of generated tokens. |
| `stream` | Whether streaming output is enabled. |
| `temperature` | Sampling temperature. |
| `top_p` | Nucleus sampling parameter. |

### Service

`Service` describes scheduling and serving requirements.

| Field | Description |
|---|---|
| `Enable_PD_Disaggregation` | Whether PD disaggregation is enabled. Reserved for future extension. |
| `Enable_know_injection` | Whether knowledge injection is enabled. |
| `Injection_type` | Knowledge injection mode, such as `text` or `kvcache`. |
| `Enable_compress` | Whether KVCache compression is enabled. Reserved for future extension. |
| `Compress_factor` | KVCache compression factor. |
| `Knowledge_block_num` | Number of knowledge blocks to retrieve. |
| `Knowledge_List` | Selected knowledge IDs. |
| `Knowledge_length` | Total token length of selected knowledge. |
| `SLO_TTFT` | Time-to-first-token SLO in milliseconds. |
| `SLO_E2E` | End-to-end latency SLO in milliseconds. |
| `SLO_TPOT` | Time-per-output-token SLO in milliseconds. |
| `Endpoint_type` | OpenAI-compatible endpoint type. |

### Task

`Task` records scheduling results and routing metadata.

| Field | Description |
|---|---|
| `P_proxy_id` | Selected Proxy ID. |
| `P_proxy_addr` | Selected Proxy address. |
| `P_proxy_port` | Selected Proxy service-plane port. |
| `KDN_server_addr` | Selected KDN server address. |
| `prefill_instance` | Selected prefill Instance. Reserved or filled by later stages. |
| `User_url_path` | Original user request endpoint path. |

## Request building workflow

```text
OpenAI-compatible request
  └──> Request.build_request()
        ├── parse endpoint type
        ├── extract user prompt
        ├── estimate prompt token length
        ├── parse generation options
        ├── parse RAG and Injection_type options
        ├── retrieve knowledge if RAG is enabled
        ├── call scheduling strategy
        └── generate Request payload for Proxy
```

Supported request formats include OpenAI-compatible `chat/completions`, `completions`, and the older `user_prompt` style.

## Knowledge retrieval

When `RAG` is enabled and the Scheduler has an initialized knowledge table, `Request.build_request()` calls the knowledge retriever to select related knowledge blocks.

Relevant configuration:

```text
SCHEDULER_RETRIEVAL_TOP_K
SCHEDULER_RETRIEVAL_MIN_SCORE
SCHEDULER_RETRIEVAL_MIN_RATIO
EMBEDDING_MODEL
KNOWLEDGE_YAML_PATH
```

The selected knowledge IDs are stored in:

```text
Request.Service.Knowledge_List
Request.Service.Knowledge_length
```

## Injection type

CacheRoute supports two main knowledge injection modes:

| Mode | Description |
|---|---|
| `text` | Fetch knowledge text and prepend or insert it into the prompt. |
| `kvcache` | Reuse prepared KVCache blocks through KDN and LMCache. |

Common aliases are normalized:

```text
kvcache, kv, kv_cache, kv-cache  -> kvcache
text, prompt                     -> text
```

If `Injection_type` is missing, CacheRoute currently defaults to `kvcache`. The Proxy may override this mode when the IWS injection strategy is enabled.

## Component address configuration

| Component | Service plane | Control plane |
|---|---|---|
| Scheduler | Receives user requests. | Receives Proxy and KDN registration / heartbeat. |
| Proxy | Receives Scheduler-forwarded requests. | Receives Instance registration, heartbeat, topology, and resource snapshots. |
| Instance | Receives Proxy-forwarded requests. | Receives KVCache injection commands. |
| KDN Server | Serves knowledge query and KVCache metadata. | Reports metadata to Scheduler. |

For multi-machine experiments, replace loopback addresses with reachable network addresses. Use `0.0.0.0` only for service binding and use the actual machine IP when advertising a component to other machines.

## Connectivity checklist

From Proxy / Instance machine:

```bash
curl http://127.0.0.1:8002/healthz
curl http://127.0.0.1:9201/healthz
```

From KDN machine:

```bash
curl http://<scheduler-host>:7002/healthz
```

Check Redis reachability from the KDN machine:

```bash
redis-cli -h <instance-host> -p 6379 ping
```

Check vLLM reachability from the Instance machine:

```bash
curl http://127.0.0.1:8000/v1/models
```

## Notes

- The default configuration is designed for single-machine demos.
- Resource monitoring is enabled by default in `demo_instance.py`, but can be disabled with `--no-resource-monitor` or `INSTANCE_RESOURCE_MONITOR_ENABLE=0`.
- KDN-to-Redis access is a common deployment error. If Redis is local to the Instance machine, KDN must use the Instance machine IP rather than `127.0.0.1`.
- Some fields in `core/config.py` are reserved for future features such as PD disaggregation, Mooncake integration, and synchronization modes.
