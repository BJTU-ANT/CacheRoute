# Client

The `client/` module provides request sending, interactive debugging, workload replay, performance measurement, and KVCache timing collection tools for CacheRoute.

It is mainly used to:

- send OpenAI-compatible requests to the Scheduler;
- validate chat/completions and completions APIs;
- run concurrent or RPS-based workload tests;
- collect CacheRoute trace metadata from Proxy responses;
- measure text injection and KVCache injection performance.

---

## Directory Structure

```text
client/
├── client.py             # Interactive OpenAI-style request REPL
├── perf_client.py        # Concurrent / RPS workload performance client
├── kv_timing_sender.py   # KVCache timing data collector
├── taskset/              # Workload JSON files
└── README.md
```

---

## Request Path

The Client sends requests to the Scheduler service plane.

```text
Client
  └──> Scheduler :7001
          └──> Proxy
                  └──> Instance / vLLM + LMCache
```

The Client does not directly contact Proxy, Instance, KDN, or vLLM in the normal CacheRoute workflow.

---

## Supported Endpoints

CacheRoute follows OpenAI-compatible endpoint formats.

| Endpoint | Description |
|---|---|
| `/v1/chat/completions` | Chat completion API. Supports streaming responses. |
| `/v1/completions` | Completion API. Usually used with non-streaming responses. |

Common CacheRoute-specific request fields:

| Field | Description |
|---|---|
| `RAG` | Enables knowledge injection when set to `true`. |
| `Injection_type` | Selects the initial injection mode. Supported values: `text`, `kvcache`, and `hybrid` in workload clients. |
| `stream` | Enables streaming output for chat completion. |
| `knowledge_id` | Optional single knowledge ID override. |
| `knowledge_ids` | Optional list or comma-separated knowledge IDs. |

The Proxy may override `Injection_type` when the IWS injection strategy is enabled.

---

## Interactive Client

`client.py` provides a simple REPL for sending OpenAI-style HTTP requests to the Scheduler.

Start the client:

```bash
python3 client/client.py
```

Inside the REPL, enter a curl-like request line.

### Chat completion example

```text
http://127.0.0.1:7001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3-70b","messages":[{"role":"user","content":"What is vLLM?"}],"max_tokens":64,"stream":true,"RAG":true,"Injection_type":"kvcache"}'
```

### Completion example

```text
http://127.0.0.1:7001/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3-70b","prompt":"What is DeepSeek?","max_tokens":64,"RAG":true,"Injection_type":"text"}'
```

Supported REPL commands:

| Command | Description |
|---|---|
| `:help` | Show usage examples. |
| `:quit` | Exit the client. |
| `:exit` | Exit the client. |

The interactive client validates request fields against the allowed OpenAI-compatible fields defined in `core/config.py`. It also parses CacheRoute metadata from streaming and non-streaming responses.

---

## Direct curl Examples

You can also send requests directly with `curl`.

### Chat completion

```bash
curl http://127.0.0.1:7001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3-70b",
    "messages": [
      {"role": "user", "content": "What is vLLM?"}
    ],
    "max_tokens": 64,
    "stream": true,
    "RAG": true,
    "Injection_type": "kvcache"
  }'
```

### Completion

```bash
curl http://127.0.0.1:7001/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3-70b",
    "prompt": "What is DeepSeek?",
    "max_tokens": 64,
    "temperature": 0,
    "RAG": true,
    "Injection_type": "text"
  }'
```

For multi-machine deployment, replace `127.0.0.1:7001` with the Scheduler service-plane address.

---

## CacheRoute Metadata

When the Proxy returns a response, it may attach CacheRoute metadata.

For streaming chat responses, the Proxy appends an SSE event before `[DONE]`:

```text
event: cacheroute_meta
data: {...}
```

For non-streaming completion responses, the metadata is returned in:

```text
_cacheroute_meta
```

The client parses this metadata and prints a compact performance summary when available.

Typical metadata fields include:

| Field | Description |
|---|---|
| `trace` | Timestamp trace collected along the Proxy pipeline. |
| `kv_ack` | KVCache injection acknowledgement from Instance / KDN. |
| `kv_ready_kids` | Knowledge IDs with prepared KVCache. |
| `text_only_kids` | Knowledge IDs available only as text. |
| `miss_kids` | Knowledge IDs not found in KDN. |
| `error` | Error message if the Proxy records a failure. |

---

## Performance Client

`perf_client.py` replays a workload file and reports request-level and average performance metrics.

It supports two modes:

| Mode | Description |
|---|---|
| `concurrent` | Sends a fixed number of requests with a maximum concurrency limit. |
| `rps` | Sends requests according to a target request rate. |

The script reads a workload JSON file with a non-empty `requests` list.

---

## Workload Format

A workload file should contain request templates:

```json
{
  "requests": [
    {
      "name": "req_001",
      "messages": [
        {"role": "user", "content": "What is vLLM?"}
      ]
    },
    {
      "name": "req_002",
      "messages": [
        {"role": "user", "content": "Explain KV cache reuse."}
      ]
    }
  ]
}
```

Each request template should contain:

| Field | Required | Description |
|---|---:|---|
| `name` | Yes | Request name used in output summaries. |
| `messages` | Yes for chat | OpenAI-style chat messages. |
| `url_path` | No | Endpoint path. Defaults to `/v1/chat/completions`. |
| `model` | No | Overrides global `--model`. |
| `RAG` | No | Overrides global `--rag`. |
| `Injection_type` | No | Overrides global `--injection-type`. |
| `knowledge_id` | No | Optional single knowledge ID. |
| `knowledge_ids` | No | Optional knowledge ID list. |
| `knowledge_length_tokens` | No | Optional explicit knowledge length for timing analysis. |

---

## Concurrent Mode

Concurrent mode sends a fixed number of requests with a concurrency limit.

```bash
python3 client/perf_client.py \
  --mode concurrent \
  --base-url http://127.0.0.1:7001 \
  --workload-file client/taskset/workload_nq.json \
  --model llama3-70b \
  --stream true \
  --rag true \
  --injection-type text \
  --max-tokens 64 \
  --temperature 0.8 \
  --top-p 1.0 \
  --requests 20 \
  --concurrency 4 \
  --seed 42
```

Important options:

| Option | Description |
|---|---|
| `--requests` | Total number of requests to send. |
| `--concurrency` | Maximum number of inflight requests in concurrent mode. |
| `--allow-duplicate` | Allows the same workload template to be sampled multiple times. |
| `--seed` | Random seed for reproducible sampling. |

---

## RPS Mode

RPS mode sends requests at a target rate.

```bash
python3 client/perf_client.py \
  --mode rps \
  --base-url http://127.0.0.1:7001 \
  --workload-file client/taskset/workload_nq.json \
  --model llama3-70b \
  --stream true \
  --rag true \
  --injection-type kvcache \
  --requests 30 \
  --rps 0.1 \
  --seed 118
```

Important options:

| Option | Description |
|---|---|
| `--rps` | Target request rate. Required in RPS mode. |
| `--requests` | Total number of requests to send. |
| `--seed` | Random seed for reproducible sampling. |

The RPS sender schedules requests according to `1 / rps` intervals and records the actual send delay for each request.

---

## Injection Modes

`perf_client.py` supports three injection modes:

| Mode | Description |
|---|---|
| `text` | Sends all requests with text-based knowledge injection. |
| `kvcache` | Sends all requests with KVCache-based knowledge injection. |
| `hybrid` | Alternates KVCache and text requests according to `--hybrid-pattern`. |

Example hybrid mode:

```bash
python3 client/perf_client.py \
  --mode rps \
  --base-url http://127.0.0.1:7001 \
  --workload-file client/taskset/workload_nq.json \
  --model llama3-70b \
  --stream true \
  --rag true \
  --injection-type hybrid \
  --hybrid-pattern 1:1 \
  --requests 30 \
  --rps 1 \
  --seed 118
```

The hybrid pattern uses the format:

```text
KVCache:text
```

For example:

| Pattern | Meaning |
|---|---|
| `1:1` | One KVCache request followed by one text request. |
| `2:1` | Two KVCache requests followed by one text request. |
| `3:1` | Three KVCache requests followed by one text request. |

`perf_client.py` validates this pattern and maps each request index to the corresponding injection type.

---

## GPU Monitoring

`perf_client.py` can sample GPU utilization through `nvidia-smi`.

```bash
python3 client/perf_client.py \
  --mode rps \
  --base-url http://127.0.0.1:7001 \
  --workload-file client/taskset/workload_nq.json \
  --model llama3-70b \
  --stream true \
  --rag true \
  --injection-type kvcache \
  --requests 30 \
  --rps 1 \
  --monitor-gpu \
  --gpu-sample-interval 1.0 \
  --gpu-ids 0,1
```

Options:

| Option | Description |
|---|---|
| `--monitor-gpu` | Enables GPU utilization sampling. |
| `--gpu-sample-interval` | GPU sampling interval in seconds. |
| `--gpu-ids` | Comma-separated GPU IDs to monitor. |

The script prints average GPU utilization, memory usage, and power statistics when valid samples are collected.

---

## Performance Metrics

`perf_client.py` extracts timing metrics from the CacheRoute trace.

Common metrics include:

| Metric | Description |
|---|---|
| `total_prefill_ms` | Time from Proxy enqueue to first token. |
| `proxy_before_vllm_ms` | Time spent inside Proxy before forwarding to vLLM / Instance. |
| `proxy_queue_wait_ms` | Queue waiting time inside Proxy. |
| `knowledge_fetch_ms` | Time spent fetching knowledge metadata from KDN. |
| `knowledge_preparation_total_ms` | Total knowledge preparation time. |
| `vllm_compute_to_first_token_ms` | Time from Instance forward start to first token. |
| `kv_ack_ms` | KVCache injection acknowledgement wait time. |
| `kv_inject_queue_wait_ms` | KVCache injection queue waiting time. |
| `kv_inject_exec_ms` | KVCache injection execution time. |
| `ready_queue_wait_ms` | Waiting time in ready queue. |
| `predict_error_ms` | Difference between actual and predicted timing when available. |

The script also checks trace completeness, missing metadata, missing first-token timestamps, and timestamp order.

---

## KVCache Timing Sender

`kv_timing_sender.py` is used to collect KVCache timing data for modeling and analysis.

It sends requests at a target RPS and writes per-request timing records to JSONL and CSV files.

Example:

```bash
python3 client/kv_timing_sender.py \
  --base-url http://127.0.0.1:7001 \
  --workload-file client/taskset/workload_nq.json \
  --model llama3-70b \
  --stream true \
  --rag true \
  --injection-type kvcache \
  --requests 30 \
  --rps 1 \
  --seed 118 \
  --scheduler-tokenizer-map '{"llama3-70b":"/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct"}' \
  --output-jsonl ./out/kv_timing.jsonl \
  --output-csv ./out/kv_timing.csv \
  --enable-scheduler-knowledge-peek true
```

Important options:

| Option | Description |
|---|---|
| `--base-url` | Scheduler service-plane URL. |
| `--workload-file` | Workload JSON path. |
| `--requests` | Total number of requests to send. |
| `--rps` | Target request rate. |
| `--injection-type` | Injection mode: `text`, `kvcache`, or `hybrid`. |
| `--kv-gb-per-token` | Estimated KVCache size per token. |
| `--output-jsonl` | Output JSONL file path. |
| `--output-csv` | Output CSV file path. |
| `--enable-scheduler-knowledge-peek` | Queries Scheduler for knowledge length when workload lacks explicit length. |
| `--scheduler-tokenizer-map` | Local tokenizer map used to avoid online tokenizer loading. |

The sender can query Scheduler `/debug/knowledge/peek` to obtain more accurate knowledge lengths when the workload does not provide `knowledge_length_tokens`.

---

## KVCache Timing Fields

`kv_timing_sender.py` records fields useful for estimating KVCache transfer and residual recomputation cost.

Typical fields include:

| Field | Description |
|---|---|
| `total_length_tokens` | Total predicted request length. |
| `knowledge_length_tokens` | Estimated knowledge length. |
| `actual_hit_length_tokens` | KVCache hit length after 256-token alignment. |
| `remaining_compute_tokens` | Tokens that still need computation. |
| `kv_size_gb` | Estimated KVCache size. |
| `queue_wait_ms` | Proxy queue waiting time. |
| `compute_ms` | Actual compute-related time from trace. |
| `text_compute_estimate_ms` | Estimated text recomputation time. |
| `lmcache_redis_pull_ms` | Estimated Redis / LMCache KV loading time. |
| `total_ms` | Total measured timing. |
| `knowledge_length_source` | Source of knowledge length, such as workload or Scheduler peek. |

The hit length is aligned to 256 tokens:

```text
actual_hit_length_tokens = floor(knowledge_length_tokens / 256) * 256
```

and is clipped so that it does not exceed the estimated knowledge length.

---

## Recommended Validation Flow

Before running large experiments, validate the Client in this order:

### 1. Start CacheRoute components

Start Scheduler, Proxy, Instance, and KDN according to the main README.

### 2. Send one request with curl

```bash
curl http://127.0.0.1:7001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3-70b",
    "messages": [{"role": "user", "content": "What is CacheRoute?"}],
    "stream": true,
    "RAG": true,
    "Injection_type": "text",
    "max_tokens": 64
  }'
```

### 3. Test the interactive client

```bash
python3 client/client.py
```

### 4. Run a small performance test

```bash
python3 client/perf_client.py \
  --mode concurrent \
  --base-url http://127.0.0.1:7001 \
  --workload-file client/taskset/workload_nq.json \
  --model llama3-70b \
  --stream true \
  --rag true \
  --injection-type text \
  --requests 4 \
  --concurrency 2 \
  --seed 42
```

### 5. Run a small KVCache timing collection

```bash
python3 client/kv_timing_sender.py \
  --base-url http://127.0.0.1:7001 \
  --workload-file client/taskset/workload_nq.json \
  --model llama3-70b \
  --stream true \
  --rag true \
  --injection-type kvcache \
  --requests 4 \
  --rps 1 \
  --output-jsonl ./out/kv_timing_test.jsonl \
  --output-csv ./out/kv_timing_test.csv
```

---

## Notes

- Use the Scheduler service-plane URL as `--base-url`.
- Use `--seed` to make workload sampling reproducible.
- Use `--allow-duplicate` when the requested number is larger than the workload size.
- Use streaming mode when you need accurate first-token timing.
- `perf_client.py` supports configurable hybrid patterns such as `1:1`, `2:1`, and `3:1`.
- `kv_timing_sender.py` is mainly intended for KVCache timing modeling and output-file generation.
- For multi-machine experiments, replace `127.0.0.1:7001` with the reachable Scheduler address.
