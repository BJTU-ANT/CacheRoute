<img width="1400" height="369" alt="CacheRoute" src="https://github.com/user-attachments/assets/6050e71f-0e37-4cf9-b712-26e11242c9cd" />

<p align="center">
  <b>Flexible KV cache reuse for knowledge-intensive LLM serving</b>
</p>

<p align="center">
  <i>Built on vLLM and LMCache. Designed for compute-network-aware knowledge injection across LLM systems.</i>
</p>

<p align="center">
  <a href="https://github.com/AstraNetLab/CacheRoute/releases">
    <img src="https://img.shields.io/badge/version-0.1.9-blue" alt="Version">
  </a>
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="License">
  </a>
  <a href="https://github.com/vllm-project/vllm">
    <img src="https://img.shields.io/badge/Built%20on-vLLM-6C5CE7?style=flat-square&logo=github&logoColor=white" alt="Built on vLLM">
  </a>
  <a href="https://github.com/LMCache/LMCache">
    <img src="https://img.shields.io/badge/Powered%20by-LMCache-00B894?style=flat-square&logo=github&logoColor=white" alt="Powered by LMCache">
  </a>
  <img src="https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/Rust-Agent-orange?logo=rust&logoColor=white" />
  <img src="https://img.shields.io/badge/FastAPI-Backend-009688?logo=fastapi&logoColor=white" />
  <img src="https://img.shields.io/badge/Redis-KV%20Store-DC382D?logo=redis&logoColor=white" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white" />
</p>

<p align="center">
  <a href="#why-cacheroute">Why CacheRoute?</a> •
  <a href="#key-features">Features</a> •
  <a href="#architecture">Architecture</a> •
  <a href="#frontend-urls">Frontend URLs</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#api-usage">API</a> •
  <a href="#documentation">Docs</a>
</p>

## CacheRoute

CacheRoute is a lightweight LLM scheduling framework built on [vLLM](https://github.com/vllm-project/vllm) and [LMCache](https://github.com/LMCache/LMCache) to enable flexible KV cache reuse across LLM systems. It targets knowledge-intensive LLM services, such as browser AI and knowledge QA systems, where many requests repeatedly use the same external knowledge. Existing systems usually prepend long knowledge texts to the user question and send the whole prompt to the model for recomputation. Although this approach helps reduce model hallucination and improve answer quality, it introduces heavy prefill overhead and redundant computation when the same knowledge appears across many requests.

CacheRoute addresses this problem by using dedicated servers to store KVCache blocks for popular knowledge. For each request, CacheRoute dynamically chooses between text-based injection and KVCache-based injection according to task queues, compute load, and network load. CacheRoute therefore shifts knowledge-injection cost between compute and network resources, improving task latency and system throughput.

## Why CacheRoute?

- 🚀 **Less redundant prefill computation:** reuse repeated knowledge through KV cache instead of recomputing long prompts.
- 🔁 **Cross-system KV cache reuse:** share reusable knowledge across LLM systems through KDN servers.
- 🌐 **Compute-network coordination:** dynamically choose between recomputation and KV cache injection based on real-time resource load.

<p align="center">
  <img width="1400" alt="CacheRoute performance overview" src=".assets/cacheroute_readme_showcase.png" />
</p>

<p align="center">
  <em>CacheRoute reduces average TTFT, improves system throughput, and enables more effective KVCache reuse under knowledge-intensive workloads.</em>
</p>

## Key Features

| Feature | Description |
|---|---|
| ⚙️ **Compute-network-aware knowledge injection** | CacheRoute dynamically chooses between text recomputation and KVCache reuse. It predicts task cost at the Proxy and selects the injection strategy based on current task queues, compute load, and network load. |
| 🧭 **Knowledge-oriented cross-system routing** | CacheRoute parses the knowledge requirement before resource-pool scheduling. The Scheduler jointly considers knowledge availability, system load, and topology information, and routes requests to the LLM system that can serve the required knowledge more efficiently. |
| 🗂️ **KDN-based KV cache management** | CacheRoute follows the Knowledge Delivery Network concept and uses dedicated KDN servers to register, store, query, and inject KV cache blocks for reusable knowledge. |
| 📊 **Proxy browser UI and Instance resource dashboard** | CacheRoute provides a browser-based Proxy observability dashboard and an optional Instance resource dashboard for control-plane state, Instance liveness, resource snapshots, topology information, and short-term trends. |

---

## Architecture

CacheRoute separates global routing, local injection decisions, and KV cache management into Scheduler, Proxy, Instance, and KDN Server components.

<p align="center">
  <img width="600" alt="CacheRoute" src="https://github.com/user-attachments/assets/9150a874-4e04-4499-821b-39a850e56db6" />
</p>

- **Scheduler:** performs global resource-pool selection and knowledge-oriented task routing.
- **Proxy:** manages local task queues, selects the knowledge-injection strategy, and exposes the main Proxy browser UI.
- **Instance:** connects CacheRoute to vLLM + LMCache and handles execution signaling.
- **KDN Server:** stores reusable knowledge and injects KVCache blocks when needed.
- **Resource Agent/Dashboard:** optionally observes local Instance resource snapshots for validation and future control-plane integration.

### Default ports

| Component | Service Plane | Control Plane / Auxiliary | UI |
|---|---|---|---|
| Scheduler | 7001 | 7002 | TBD |
| Proxy | 8001 | 8002 | 8202 |
| Client UI | - | - | 7071 |
| Instance | 9001 | 9002 | 9202 |
| vLLM | 8000 | - | - |
| KDN Server | 9101 | - | TBD |

### Frontend URLs

| Component | Frontend | Default URL | How to start | Status |
|---|---|---|---|---|
| Proxy | Proxy browser observability dashboard | `http://127.0.0.1:8202` | `cd test && python3 demo_proxy.py ...` starts it by default. Use `--no-proxy-ui` to disable it. | Available |
| Instance | Browser resource dashboard | `http://127.0.0.1:9202` | `python3 instance/resource_dashboard/dashboard_server.py --dashboard-listen 0.0.0.0:9202 --agent-listen 127.0.0.1:9201` | Available |
| Client | Browser request UI | `http://127.0.0.1:7071/ui/client` | `cd test && python3 demo_client.py --with-ui` | Available |
| Scheduler | Scheduler browser UI | TBD | TBD | Planned |
| KDN Server | KDN browser UI | TBD | TBD | Planned |

The URLs above assume a single-machine deployment with loopback addresses. Containers without host networking must publish the corresponding ports or use reachable host addresses.

### System Workflow

1. The Client sends an OpenAI-compatible request to the Scheduler.
2. The Scheduler analyzes the knowledge requirement and selects a target resource pool.
3. The Proxy predicts the cost of text-based and KVCache-based injection.
4. The KDN Server injects reusable KVCache blocks when KVCache reuse is selected.
5. The Instance forwards the request to vLLM + LMCache and returns the response.
6. Instance resource snapshots can flow through the Proxy control plane. The Proxy aggregates a compact `pool_resource` snapshot and reports it to the Scheduler through registration and heartbeat payloads.
7. The optional Proxy UI and Instance Resource Dashboard visualize control-plane and resource state for debugging and validation.

---

## Requirements

CacheRoute has been tested with the following core environment:

| Component | Version |
|---|---|
| Python | 3.12.x |
| CUDA toolkit in container | 12.8 |
| PyTorch | 2.9.1 |
| vLLM | 0.13.x |
| LMCache | 0.3.11 |
| Redis | 7 |
| Rust/Cargo | Stable toolchain; required only for `instance/resource_agent` |
| Tkinter | `python3.12-tk`; required only for the desktop dashboard |

Install CacheRoute's application dependencies with:

```bash
python3 -m pip install -r requirements.txt
python3 -m pip check
```

`requirements.txt` intentionally does not pin PyTorch, vLLM, or LMCache. These packages belong to the serving image and must remain compatible with its CUDA environment.

For the complete Docker, source-build, Rust, Tkinter, X11, Redis, and LMCache setup, use [`env/README.md`](env/README.md) as the deployment source of truth.

---

## Quick Start

CacheRoute provides two ways to get started.

### Option 1: Lightweight Demo without a vLLM model

Set `USE_MOCK = True` in `core/config.py`, then start the demo components in separate terminals. The main demo entrypoints can be executed directly from `test/`; they add the repository root to Python's module search path automatically.

```bash
cd test

python3 demo_scheduler.py --cacheroute
python3 demo_kdn.py
python3 demo_proxy.py \
  --strategy round_robin \
  --injection-strategy iws \
  --ready-release-policy text_bypass
python3 demo_instance.py --port 9001 --host 127.0.0.1
python3 demo_client.py --with-ui
```

`demo_proxy.py` starts the Proxy browser UI at a URL similar to `http://127.0.0.1:8202`. `demo_client.py --with-ui` starts the Client UI at `http://127.0.0.1:7071/ui/client`.

### Option 2: Full CacheRoute deployment

The recommended host/container paths are:

```text
Host repository:      /llm-stack/CacheRoute
Container repository: /workspace/llm-stack/CacheRoute
```

When a compatible complete image already exists, create the headless/browser-mode container with:

```bash
sudo docker run --gpus all -it \
  --name cacheroute-main \
  --network host \
  --ipc=host \
  --shm-size=64g \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  --memory=0 \
  --memory-swap=0 \
  -v /llm-stack:/workspace/llm-stack \
  -w /workspace/llm-stack \
  cacheroute:vllm0.13-lmcache3.11-pytorch2.9.1 \
  bash
```

Use [`env/README.md`](env/README.md) when building the image, installing PyTorch/vLLM/LMCache from source, enabling Tkinter/X11, repairing an older image, or recreating a deleted container.

<details>
<summary><b>Full single-machine run: Redis → vLLM → Scheduler → KDN → Proxy → Instance → Client</b></summary>

The commands below assume:

- the complete image and `cacheroute-main` container already exist;
- the repository is mounted at `/workspace/llm-stack/CacheRoute`;
- all CacheRoute components use host networking and loopback addresses;
- the example model is served as `llama3-70b`;
- each long-running service is started in a separate terminal with `sudo docker exec -it cacheroute-main bash`.

#### 1. Check the CacheRoute configuration

In `core/config.py`, verify at least the following values before starting the services:

```python
DEFAULT_MODEL = "/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct"
DEFAULT_MODEL_SHORTNAME = "llama3-70b"
EMBEDDING_MODEL = "/workspace/llm-stack/CacheRoute/model/embedder/intfloat__multilingual-e5-large-instruct"
USE_MOCK = False
```

Install the CacheRoute application dependencies once inside the container:

```bash
cd /workspace/llm-stack/CacheRoute
python3 -m pip install -r requirements.txt
python3 -m pip check
```

#### 2. Start Redis on the host

Create the Redis container the first time:

```bash
sudo docker run -d \
  --name lmcache-redis \
  --network host \
  redis:7 \
  redis-server \
    --bind 0.0.0.0 \
    --protected-mode no \
    --save "" \
    --appendonly no \
    --maxmemory 200gb \
    --maxmemory-policy allkeys-lru
```

For later runs, use:

```bash
sudo docker start lmcache-redis
```

Verify Redis:

```bash
sudo docker exec -it lmcache-redis redis-cli ping
# Expected: PONG
```

#### 3. Create the LMCache configuration inside `cacheroute-main`

```bash
mkdir -p /workspace/llm-stack/config

cat > /workspace/llm-stack/config/lmcache_with_redis.yaml <<'EOF'
chunk_size: 256
pre_caching_hash_algorithm: "sha256_cbor"

local_cpu: true
max_local_cpu_size: 80.0

remote_url: "redis://127.0.0.1:6379"
remote_serde: "cachegen"

local_disk: null
max_local_disk_size: 0

save_decode_cache: false
cache_policy: "LRU"
numa_mode: null
EOF
```

#### 4. Terminal 1: start vLLM with LMCache

The following example uses eight GPUs for a LLaMA-70B model. Adjust `CUDA_VISIBLE_DEVICES`, tensor parallelism, model path, memory utilization, and batch limits for the actual machine.

```bash
sudo docker exec -it cacheroute-main bash

export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export MODEL_DIR=/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct
export LMCACHE_CONFIG_FILE=/workspace/llm-stack/config/lmcache_with_redis.yaml
export PYTHONHASHSEED=0
export OMP_NUM_THREADS=8

python3 -m vllm.entrypoints.openai.api_server \
  --model "$MODEL_DIR" \
  --served-model-name llama3-70b \
  --host 0.0.0.0 \
  --port 8000 \
  --tensor-parallel-size 8 \
  --gpu-memory-utilization 0.75 \
  --dtype auto \
  --max-model-len 4096 \
  --max-num-seqs 8 \
  --max-num-batched-tokens 16384 \
  --kv-offloading-backend lmcache \
  --kv-offloading-size 64 \
  --disable-hybrid-kv-cache-manager \
  --kv-cache-metrics
```

From another terminal, wait until the model endpoint is ready:

```bash
curl -sS http://127.0.0.1:8000/v1/models | python3 -m json.tool
```

#### 5. Terminal 2: start the Scheduler

```bash
sudo docker exec -it cacheroute-main bash
cd /workspace/llm-stack/CacheRoute/test

python3 demo_scheduler.py \
  --cacheroute \
  --kdn-pending-overload-th 8 \
  --kdn-active-overload-th 4 \
  --kdn-queue-ms-overload-th 30 \
  --cacheroute-log-decision 1
```

The Scheduler service plane listens on `127.0.0.1:7001`, and its control plane listens on `127.0.0.1:7002`.

#### 6. Terminal 3: start the KDN Server

```bash
sudo docker exec -it cacheroute-main bash
cd /workspace/llm-stack/CacheRoute/test
python3 demo_kdn.py
```

The KDN Server listens on `127.0.0.1:9101` and registers its runtime state with the Scheduler.

#### 7. Terminal 4: register sample knowledge and build its KVCache

Create a small knowledge file inside the shared workspace:

```bash
sudo docker exec -it cacheroute-main bash
cd /workspace/llm-stack/CacheRoute
mkdir -p data/quickstart

cat > data/quickstart/cacheroute.txt <<'EOF'
CacheRoute is a knowledge-oriented LLM scheduling framework. It reduces repeated prefill computation by storing and reusing KVCache blocks for frequently requested external knowledge. The Scheduler selects a KDN server and a Proxy, while the Proxy chooses between text recomputation and KVCache reuse.
EOF
```

Start the interactive KDN CLI:

```bash
python3 kdn_server/kdn_register_cli.py
```

At the CLI prompt, register the file and build its KVCache:

```text
:buildkv_file /workspace/llm-stack/CacheRoute/data/quickstart/cacheroute.txt --api-url http://127.0.0.1:8000/v1/chat/completions --model llama3-70b
:pool
```

The Scheduler automatically refreshes KDN knowledge metadata. With the default configuration, allow up to 30 seconds for the new item to appear. Check the Scheduler state from another terminal:

```bash
curl -sS http://127.0.0.1:7001/debug/status | python3 -m json.tool
```

#### 8. Terminal 5: start the Proxy

```bash
sudo docker exec -it cacheroute-main bash
cd /workspace/llm-stack/CacheRoute/test

python3 demo_proxy.py \
  --strategy round_robin \
  --injection-strategy iws \
  --ready-release-policy text_bypass
```

The Proxy service plane listens on `127.0.0.1:8001`, its control plane listens on `127.0.0.1:8002`, and the browser observability UI is available at:

```text
http://127.0.0.1:8202
```

#### 9. Terminal 6: start the Instance

```bash
sudo docker exec -it cacheroute-main bash
cd /workspace/llm-stack/CacheRoute/test

python3 demo_instance.py \
  --host 127.0.0.1 \
  --port 9001 \
  --proxy-cp-url http://127.0.0.1:8002
```

The Instance forwards inference to vLLM at `127.0.0.1:8000`. It also starts or reuses the Rust Resource Agent by default. To skip resource monitoring during a minimal functional test, add `--no-resource-monitor`.

Before sending a request, confirm that the Scheduler sees the KDN and Proxy and that the Proxy sees the Instance:

```bash
curl -sS http://127.0.0.1:7001/debug/status | python3 -m json.tool
curl -sS http://127.0.0.1:8002/v1/instance/list?include_dead=true | python3 -m json.tool
```

#### 10. Terminal 7: start the Client UI or CLI

Browser UI:

```bash
sudo docker exec -it cacheroute-main bash
cd /workspace/llm-stack/CacheRoute/test
python3 demo_client.py --with-ui
```

Open:

```text
http://127.0.0.1:7071/ui/client
```

For terminal-only use, run:

```bash
python3 demo_client.py
```

#### 11. Send a simple end-to-end request

Send the request to the Scheduler rather than directly to vLLM:

```bash
curl http://127.0.0.1:7001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3-70b",
    "messages": [
      {
        "role": "user",
        "content": "What does CacheRoute reuse to reduce repeated prefill computation?"
      }
    ],
    "temperature": 0,
    "max_tokens": 64,
    "stream": false,
    "RAG": true
  }'
```

Inspect the most recent routing decision:

```bash
curl -sS http://127.0.0.1:7001/debug/strategy | python3 -m json.tool
```

The expected request path is:

```text
Client/curl
  -> Scheduler :7001
  -> selected Proxy :8001
  -> Instance :9001
  -> vLLM + LMCache :8000
  -> response returned through the same chain
```

</details>

See [`kdn_server/README.md`](kdn_server/README.md) for detailed knowledge registration and KVCache injection commands, and [`core/README.md`](core/README.md) for multi-machine configuration.

### Optional: Instance Resource Dashboard

Build-check the Rust resource agent:

```bash
cargo check --manifest-path instance/resource_agent/Cargo.toml
```

Start the Tkinter desktop dashboard only when the image contains `python3.12-tk` and the container was created with X11 forwarding as documented in `env/README.md`:

```bash
python3 instance/resource_dashboard/dashboard_app.py \
  --agent-listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

Use the browser fallback in headless environments:

```bash
python3 instance/resource_dashboard/dashboard_server.py \
  --dashboard-listen 0.0.0.0:9202 \
  --agent-listen 127.0.0.1:9201
```

Open:

```text
http://127.0.0.1:9202
```

The resource dashboard is a validation helper and does not change Scheduler, Proxy, Instance, or KDN behavior.

---

## API Usage

CacheRoute exposes OpenAI-compatible API endpoints through the Scheduler.

| Endpoint | Mode |
|---|---|
| `/v1/chat/completions` | Chat completion |
| `/v1/completions` | Completion |

### Chat Completion

```bash
curl http://127.0.0.1:7001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3-70b",
    "messages": [{"role": "user", "content": "What is DeepSeek"}],
    "max_tokens": 1,
    "stream": false,
    "RAG": true
  }'
```

### Completion

```bash
curl http://127.0.0.1:7001/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3-70b",
    "prompt": "What is DeepSeek",
    "max_tokens": 1,
    "RAG": true
  }'
```

### Request Options

| Option | Required | Description |
|---|---|---|
| `model` | Yes | Model name served by vLLM. |
| `messages` / `prompt` | Yes | Input content for chat or completion mode. |
| `max_tokens` | No | Maximum number of generated tokens. |
| `stream` | No | Whether to enable streaming responses. |
| `RAG` | No | Whether to enable knowledge injection. |

---

## Demo Screenshots

<details>
<summary>View runtime screenshots</summary>

### Scheduler task scheduling

The Scheduler selects KDN and Proxy according to knowledge coverage, topology, and current load.
<img width="1200" height="559" alt="image" src="https://github.com/user-attachments/assets/320b5058-04b2-4de3-aa3b-aaa714b69982" />

### Proxy task scheduling

The Proxy maintains local task queues and prepares requests for instance-level execution.
<img width="1200" height="288" alt="image" src="https://github.com/user-attachments/assets/bc24230e-0167-469b-9e6a-a7be9f5d26f0" />

### Injection strategy selection

The Proxy dynamically chooses between text-based injection and KVCache-based injection.
<img width="1200" height="746" alt="image" src="https://github.com/user-attachments/assets/930575a6-dba2-465d-aff2-b511099ae25a4" />

### vLLM + LMCache reuse

The Instance reuses injected KVCache blocks through LMCache.
<img width="1200" height="224" alt="image" src="https://github.com/user-attachments/assets/558be19f-c801-4182-b9cd-7daee7fd0a80" />

### Client response

The Client receives OpenAI-compatible responses through the Scheduler endpoint.
<img width="1200" height="374" alt="image" src="https://github.com/user-attachments/assets/320b5058-04b2-4de3-aa3b-aaa714b69982" />

</details>

---

## Current Status

CacheRoute is under active development. The current release supports:

- Scheduler-side knowledge-oriented routing.
- KDN selection based on knowledge coverage and overload filtering.
- Proxy selection based on topology, load safety window, and knowledge history.
- Proxy-side dynamic injection strategy selection.
- KDN-based text registration and KVCache registration.
- Proxy browser UI for control-plane, topology, Instance liveness, and resource-snapshot observability.
- Optional Instance resource snapshots through a Rust agent and dashboard.
- Debugging APIs such as `/debug/status` and `/debug/strategy`.

Suggested minimum validation commands:

```bash
cd test
python3 demo_scheduler.py --cacheroute
curl -s http://127.0.0.1:7001/debug/status
curl -s http://127.0.0.1:7001/debug/strategy
```

### Roadmap

- [x] Scheduler-side knowledge-oriented routing
- [x] Proxy-side dynamic injection strategy selection
- [x] KDN-based text and KVCache registration
- [x] OpenAI-compatible request forwarding
- [x] Proxy browser observability UI
- [x] Optional Instance resource dashboard
- [ ] Scheduler browser UI
- [ ] KDN Server browser UI
- [ ] More deployment examples
- [ ] Benchmark scripts and reproducible evaluation
- [ ] More KV cache placement policies
- [ ] Paper and citation release

---

## Documentation

| Document | Description |
|---|---|
| [`core/README.md`](core/README.md) | Shared configuration, request model, and multi-machine deployment settings. |
| [`scheduler/README.md`](scheduler/README.md) | Global routing, KDN / Proxy pool management, and Scheduler control plane. |
| [`proxy/README.md`](proxy/README.md) | Local Instance pool, prepare / ready queues, injection strategy, and Proxy resource APIs. |
| [`instance/README.md`](instance/README.md) | Instance service and control planes, KVCache signaling, resource monitoring, and TTFT predictor. |
| [`kdn_server/README.md`](kdn_server/README.md) | KDN service, knowledge registration, KVCache build, and injection utilities. |
| [`client/README.md`](client/README.md) | Client CLI, OpenAI-compatible request examples, and workload tools. |
| [`env/README.md`](env/README.md) | Docker environment setup and vLLM + LMCache installation. |
| [`test/README.md`](test/README.md) | Demo scripts, smoke-validation entry points, and local test helpers. |
| [`doc/blog/README.md`](doc/blog/README.md) | Engineering changelog and milestone notes. |
