# Core

The `core/` module contains shared configuration, request data structures, and forwarding utilities used by Scheduler, Proxy, Instance, and KDN Server.

It provides the common runtime interface that connects CacheRoute components together.

---

## Module Overview

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

---

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
| Instance | Instance service plane, control plane, vLLM URL, Redis, and KDN topology settings. |
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

KDN Server:               http://127.0.0.1:9101
vLLM backend:             http://127.0.0.1:8000
Redis:                    127.0.0.1:6379
```

---

## Request Model

CacheRoute converts user requests into an internal `Request` object before scheduling.

The internal request contains three main parts:

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
| `Enable_security` | Whether security mode is enabled. Reserved for future extension. |
| `Knowledge_block_num` | Number of knowledge blocks to retrieve. |
| `Knowledge_List` | Selected knowledge IDs. |
| `Knowledge_length` | Total token length of selected knowledge. |
| `SLO_TTFT` | Time-to-first-token SLO in milliseconds. |
| `SLO_E2E` | End-to-end latency SLO in milliseconds. |
| `SLO_TPOT` | Time-per-output-token SLO in milliseconds. |
| `Endpoint_type` | OpenAI-compatible endpoint type, such as `chat/completions` or `completions`. |

### Task

`Task` records scheduling results and routing metadata.

| Field | Description |
|---|---|
| `User_addr` | User address. |
| `KDN_server_addr` | Selected KDN server address. |
| `default_know_addr` | Default knowledge source address. |
| `P_proxy_id` | Selected Proxy ID. |
| `P_proxy_addr` | Selected Proxy address. |
| `P_proxy_port` | Selected Proxy service-plane port. |
| `D_proxy_addr` | Decode Proxy address. Reserved for PD extension. |
| `D_proxy_port` | Decode Proxy port. Reserved for PD extension. |
| `prefill_instance` | Selected prefill Instance. Reserved or filled by later stages. |
| `decode_instance` | Selected decode Instance. Reserved for PD extension. |
| `batch_order` | Reserved batch order field. |
| `User_url_path` | Original user request endpoint path. |

---

## Request Building Workflow

`Request.build_request()` is used by the Scheduler to convert a user request into a CacheRoute internal request.

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

Supported request formats:

### Chat completion

```json
{
  "model": "llama3-70b",
  "messages": [
    {"role": "user", "content": "Tell me about CacheRoute."}
  ],
  "RAG": true,
  "Injection_type": "kvcache",
  "max_tokens": 128,
  "temperature": 0,
  "stream": true
}
```

### Completion

```json
{
  "model": "llama3-70b",
  "prompt": "Tell me about CacheRoute.",
  "RAG": true,
  "Injection_type": "text",
  "max_tokens": 128,
  "temperature": 0
}
```

### Legacy format

```json
{
  "model": "llama3-70b",
  "user_prompt": "Tell me about CacheRoute."
}
```

---

## Knowledge Retrieval

When `RAG` is enabled and the Scheduler has an initialized knowledge table, `Request.build_request()` calls the knowledge retriever to select related knowledge blocks.

The retriever:

1. encodes the user prompt into an embedding;
2. searches the knowledge table;
3. filters results by score and ratio thresholds;
4. returns selected knowledge IDs and total knowledge length.

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

These fields are later used by the Scheduler, Proxy, and KDN Server.

---

## Injection Type

CacheRoute supports two main knowledge injection modes:

| Mode | Description |
|---|---|
| `text` | Fetch knowledge text and prepend or insert it into the prompt. |
| `kvcache` | Reuse prepared KVCache blocks through KDN and LMCache. |

The request parser normalizes common aliases:

```text
kvcache, kv, kv_cache, kv-cache  -> kvcache
text, prompt                     -> text
```

If `Injection_type` is missing, CacheRoute currently defaults to:

```text
kvcache
```

The Proxy may later override this mode when the IWS injection strategy is enabled.

---

## Component Address Configuration

CacheRoute components use both service-plane and control-plane addresses.

| Component | Service Plane | Control Plane |
|---|---|---|
| Scheduler | Receives user requests. | Receives Proxy and KDN registration / heartbeat. |
| Proxy | Receives Scheduler-forwarded requests. | Receives Instance registration / heartbeat and topology reports. |
| Instance | Receives Proxy-forwarded inference requests. | Receives KVCache injection commands. |
| KDN Server | Serves knowledge query and KVCache metadata. | Reports metadata to Scheduler. |

Default single-machine configuration:

```python
SCHEDULER_BASE_URL = "http://127.0.0.1:7001"
SCHEDULER_CP_URL   = "http://127.0.0.1:7002"
SCHEDULER_DP_HOST  = "127.0.0.1"
SCHEDULER_CP_HOST  = "127.0.0.1"

PROXY_BASE_URL = "http://127.0.0.1:8001"
PROXY_CP_URL   = "http://127.0.0.1:8002"
PROXY_DP_HOST  = "127.0.0.1"
PROXY_CP_HOST  = "127.0.0.1"

INSTANCE_BASE_URL = "http://127.0.0.1:9001"
INSTANCE_HOST     = "127.0.0.1"
INSTANCE_CP_HOST  = "127.0.0.1"

KDN_BASE_URL = "http://127.0.0.1:9101"
KDN_HOST     = "127.0.0.1"
```

---

## Multi-machine Deployment Example

For multi-machine experiments, replace loopback addresses with reachable network addresses.

Example setup:

```text
Inference server: 172.18.0.169
KDN server:       172.18.0.171
```

In this example, Scheduler, Proxy, Instance, vLLM, and Redis run on `172.18.0.169`, while KDN runs on `172.18.0.171`.

### On the inference server: 172.18.0.169

Configure Scheduler:

```python
SCHEDULER_BASE_URL = "http://172.18.0.169:7001"
SCHEDULER_CP_URL   = "http://172.18.0.169:7002"

SCHEDULER_DP_HOST = "0.0.0.0"
SCHEDULER_CP_HOST = "0.0.0.0"
```

Configure Proxy:

```python
PROXY_BASE_URL = "http://172.18.0.169:8001"
PROXY_CP_URL   = "http://172.18.0.169:8002"

PROXY_DP_HOST = "0.0.0.0"
PROXY_CP_HOST = "0.0.0.0"
```

Configure Instance:

```python
INSTANCE_BASE_URL = "http://172.18.0.169:9001"

INSTANCE_HOST    = "0.0.0.0"
INSTANCE_CP_HOST = "0.0.0.0"

INSTANCE_REDIS_HOST = "127.0.0.1"
INSTANCE_REDIS_PORT = 6379

INSTANCE_TOPOLOGY_KDN_TARGETS = "http://172.18.0.171:9101"
```

If KDN needs to inject KVCache into the Redis backend on the inference server, make sure Redis is reachable from the KDN server. In that case, `127.0.0.1` should not be used from the KDN side.

### On the KDN server: 172.18.0.171

Configure KDN:

```python
KDN_BASE_URL = "http://172.18.0.171:9101"
KDN_HOST     = "0.0.0.0"
KDN_PORT     = 9101

SCHEDULER_CP_URL = "http://172.18.0.169:7002"
```

If the upstream request passes Redis as `127.0.0.1`, enable Redis host rewriting so that KDN connects to the Redis service on the inference server:

```python
KDN_REDIS_REWRITE_ENABLE = True
KDN_FORCE_REDIS_HOST = "172.18.0.169"
```

or use environment variables:

```bash
export KDN_REDIS_REWRITE_ENABLE=1
export KDN_FORCE_REDIS_HOST=172.18.0.169
```

---

## Environment Variable Overrides

Many runtime settings can be overridden by environment variables before starting each component.

Example:

```bash
export SCHEDULER_CP_URL=http://172.18.0.169:7002
export PROXY_ADVERTISE_HOST=172.18.0.169
export PROXY_ADVERTISE_PORT=8001
export KDN_FORCE_REDIS_HOST=172.18.0.169
```

This is useful when running the same codebase across multiple machines or containers.

---

## Connectivity Checklist

Before running a multi-machine experiment, check the following connections.

From Proxy / Instance machine:

```bash
curl http://172.18.0.171:9101/healthz
```

From KDN machine:

```bash
curl http://172.18.0.169:7002/healthz
```

Check Redis reachability from the KDN machine:

```bash
redis-cli -h 172.18.0.169 -p 6379 ping
```

Check vLLM reachability from the Instance machine:

```bash
curl http://127.0.0.1:8000/v1/models
```

If these checks fail, verify firewall rules, Docker network mode, host binding addresses, and Redis binding configuration.

---

## Notes

- The default configuration is designed for single-machine demos.
- For multi-machine experiments, replace all externally accessed `127.0.0.1` addresses with reachable host IPs.
- Use `0.0.0.0` only for service binding. Use the actual machine IP when advertising a component to other machines.
- KDN-to-Redis access is a common source of deployment errors. If Redis is local to the Instance machine, KDN must use the Instance machine IP rather than `127.0.0.1`.
- Some fields in `core/config.py` are reserved for future features such as PD disaggregation, Mooncake integration, and synchronization modes.

### Core
涉及关键方法、结构体定义

- config.py:关键配置信息接口
- fwd.py: vLLM自带的http请求收发接口
- model_calculation.py:
