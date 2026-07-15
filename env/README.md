# Environment Setup

This document describes the environment used to run CacheRoute with vLLM, LMCache, Redis, CUDA GPUs, and the optional Instance Resource Agent/Dashboard.

CacheRoute can run a lightweight mock demo without a full LLM serving stack. This document is for the full deployment path, where vLLM + LMCache are built and used as the backend inference engine.

---

## Tested Environment

CacheRoute has been tested with the following environment:

| Component | Version |
|---|---|
| OS | Ubuntu 22.04.5 Jammy |
| Docker | 28.2.2 |
| CUDA | 13.0 |
| NVIDIA Driver | 580.95.05 |
| PyTorch | 2.9.1 |
| vLLM | 0.13.x |
| LMCache | 0.3.x |
| Python | 3.12.x |
| Rust | stable toolchain |
| Tkinter | `python3.12-tk` for the optional desktop dashboard |

The exact versions can be adjusted, but the vLLM, LMCache, CUDA, PyTorch, and driver versions should be kept compatible.

Rust is required only for the native Instance Resource Agent under `instance/resource_agent`. Tkinter is required only when using the local desktop dashboard under `instance/resource_dashboard/dashboard_app.py`.

---

## Workspace Layout

CacheRoute assumes a shared workspace for source code, models, cache files, and logs.

A recommended layout is:

```text
/llm-stack/
├── src/                         # vLLM, LMCache, and CacheRoute source code
├── models/                      # local model files
├── cache/
│   ├── hf/
│   ├── torch/
│   └── lmcache/
│       └── local_disk/
├── logs/
└── docker/
```

Create the workspace:

```bash
sudo mkdir -p /llm-stack
sudo mkdir -p /llm-stack/{src,models,cache/{hf,torch,lmcache/local_disk},logs,docker}
```

Clone the required repositories:

```bash
cd /llm-stack/src

git clone https://github.com/vllm-project/vllm.git
git clone https://github.com/LMCache/LMCache.git
git clone https://github.com/BJTU-ANT/CacheRoute.git
```

---

## Docker Setup

### Install Docker

```bash
sudo apt --fix-broken install
sudo apt install docker.io
sudo apt install apt-transport-https ca-certificates curl software-properties-common
```

### Install NVIDIA container runtime

```bash
sudo apt install nvidia-container-runtime
sudo systemctl restart docker
```

Check whether the GPU is visible on the host:

```bash
nvidia-smi
```

---

## Build the Base Docker Image

CacheRoute provides a Dockerfile under:

```text
CacheRoute/env/docker/Dockerfile
```

Build the base CUDA image:

```bash
cd /llm-stack/src/CacheRoute/env/docker
sudo docker build -t basic-cu128 .
```

The Dockerfile installs the Rust stable toolchain for the Instance Resource Agent and `python3.12-tk` for the optional desktop dashboard. The image name can be changed according to your local environment. Make sure the CUDA version in the image matches your driver and PyTorch version.

---

## Start the Development Container

Create a container for building and running vLLM + LMCache:

```bash
sudo docker run --gpus all -it \
  --name cacheroute-dev \
  --network host \
  --ipc=host \
  --shm-size=64g \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  --memory=0 \
  --memory-swap=0 \
  -v /llm-stack:/workspace/llm-stack \
  -w /workspace/llm-stack \
  basic-cu128 bash
```

Useful options:

| Option | Description |
|---|---|
| `--gpus all` | Exposes all host GPUs to the container. |
| `--network host` | Uses host networking, which simplifies multi-service communication and exposes the dashboard at `127.0.0.1:9202`. |
| `--ipc=host` | Shares IPC namespace with the host. |
| `--shm-size=64g` | Provides enough shared memory for large-model serving. |
| `--ulimit memlock=-1` | Allows memory locking. |
| `-v /llm-stack:/workspace/llm-stack` | Mounts the workspace into the container. |

If the container does not use host networking, map the dashboard port explicitly, for example `-p 9202:9202`.

Restart and enter the container later:

```bash
sudo docker start cacheroute-dev
sudo docker exec -it cacheroute-dev bash
```

---

## Verify Rust and Tkinter

Inside the container, verify that Rust is available:

```bash
rustc --version
cargo --version
```

Verify that Tkinter is available for the desktop dashboard:

```bash
python3 - <<'EOF'
import tkinter
print("tkinter: ok")
EOF
```

If you use a custom image instead of `env/docker/Dockerfile`, install Rust and Tkinter manually before using `instance/resource_agent` or `instance/resource_dashboard/dashboard_app.py`.

---

## Install PyTorch

Inside the container, install PyTorch with CUDA support:

```bash
python3 -m pip install -U pip
python3 -m pip install --index-url https://download.pytorch.org/whl/cu128 torch torchvision torchaudio
```

Verify GPU availability:

```bash
python3 - <<'EOF'
import torch

print("torch:", torch.__version__)
print("cuda available:", torch.cuda.is_available())
print("cuda:", torch.version.cuda)
print("n_gpus:", torch.cuda.device_count())
EOF
```

---

## Install CacheRoute Dependencies

From the CacheRoute root directory:

```bash
cd /workspace/llm-stack/src/CacheRoute
python3 -m pip install -r requirements.txt
python3 -m pip check
```

Optional tools for model downloading and API testing:

```bash
python3 -m pip install modelscope openai
```

---

## Optional: Instance Resource Dashboard

The Instance Resource Dashboard starts or connects to the Rust resource agent and visualizes local Instance resource snapshots.

Build/check the Rust agent:

```bash
cd /workspace/llm-stack/src/CacheRoute
cargo check --manifest-path instance/resource_agent/Cargo.toml
```

Start the desktop dashboard:

```bash
python3 instance/resource_dashboard/dashboard_app.py \
  --agent-listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

A small local window should open and update CPU, memory, GPU, network, admission-state, and raw snapshot information.

If the container cannot open the desktop window, you can also try running the Rust agent and desktop dashboard directly on the host machine, as long as the host has a compatible Rust/Cargo toolchain and repository permissions are correct.

If the container has no graphical display, use the browser/server fallback:

```bash
python3 instance/resource_dashboard/dashboard_server.py \
  --dashboard-listen 0.0.0.0:9202 \
  --agent-listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

Validate the fallback APIs:

```bash
curl -sS http://127.0.0.1:9202/api/health | python3 -m json.tool
curl -sS http://127.0.0.1:9202/api/snapshot | python3 -m json.tool
curl -sS http://127.0.0.1:9201/healthz
curl -sS http://127.0.0.1:9201/v1/resource/snapshot | python3 -m json.tool
```

The dashboard is optional and does not change Scheduler decisions, Proxy forwarding, Instance request handling, or KDN injection.

---

## Build vLLM from Source

CacheRoute has been tested with vLLM 0.13.x.

```bash
cd /workspace/llm-stack/src/vllm

git checkout v0.13.0
python3 use_existing_torch.py

python3 -m pip install -U pip setuptools wheel packaging
python3 -m pip install -r requirements/build.txt

export MAX_JOBS=8
python3 -m pip install --no-build-isolation -e .
```

This step may take a long time. If the build appears stuck, check CPU and memory usage:

```bash
ps -eo pid,ppid,cmd,%cpu,%mem --sort=-%cpu | head -n 30
```

---

## Configure the Chat Template

Some LLaMA-family models do not include a proper chat template. You can reuse the template provided by vLLM.

Example:

```bash
export VLLM_DIR=/workspace/llm-stack/src/vllm
export TEMPLATE=$VLLM_DIR/examples/tool_chat_template_llama3.2_json.jinja
export MODEL_DIR=/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct

python3 - <<'EOF'
import json
import os
from pathlib import Path

model_dir = Path(os.environ["MODEL_DIR"])
tpl_path = Path(os.environ["TEMPLATE"])

assert model_dir.is_dir(), model_dir
assert tpl_path.is_file(), tpl_path

tpl = tpl_path.read_text(encoding="utf-8")
cfg = model_dir / "tokenizer_config.json"

data = {}
if cfg.exists():
    data = json.loads(cfg.read_text(encoding="utf-8"))

data["chat_template"] = tpl
cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

print("OK wrote:", cfg)
print("template chars:", len(tpl))
EOF
```

For stable KVCache reuse, the chat template should not contain time-varying fields such as the current date. Otherwise, the generated cache keys may change over time. CacheRoute provides an example modified tokenizer configuration under:

```text
env/tokenizer_config.json
```

---

## Install LMCache

Install LMCache from source:

```bash
cd /workspace/llm-stack/src/LMCache
python3 -m pip install -e .
```

Verify installation:

```bash
python3 -m pip show lmcache
```

---

## Configure LMCache

Create a local LMCache configuration file:

```bash
mkdir -p /workspace/llm-stack/config
mkdir -p /workspace/llm-stack/lmcache-data

cat > /workspace/llm-stack/config/lmcache.yaml <<'EOF'
chunk_size: 256

local_cpu: true
max_local_cpu_size: 64.0

local_disk: "file:///workspace/llm-stack/lmcache-data"
max_local_disk_size: 200.0

remote_url: null

save_decode_cache: false
cache_policy: "LRU"
numa_mode: null
EOF
```

For CacheRoute experiments with Redis-based KVCache injection, use a Redis backend configuration instead. See the main `README.md` and `kdn_server/README.md` for Redis-based KDN injection.

---

## Start vLLM

Example command for starting a LLaMA-70B model with tensor parallelism:

```bash
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
export MODEL_DIR=/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct
export LMCACHE_CONFIG_FILE=/workspace/llm-stack/config/lmcache.yaml

python3 -m vllm.entrypoints.openai.api_server \
  --model "$MODEL_DIR" \
  --served-model-name llama3-70b \
  --host 0.0.0.0 \
  --port 8000 \
  --tensor-parallel-size 8 \
  --gpu-memory-utilization 0.84 \
  --dtype auto \
  --max-model-len 4096 \
  --max-num-seqs 8 \
  --max-num-batched-tokens 8192
```

Adjust `MODEL_DIR`, tensor parallel size, memory utilization, and batch limits according to your model and GPU resources.

---

## Verify vLLM

### Chat completion

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3-70b",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Tell me a short story about a sailor."}
    ],
    "temperature": 0,
    "max_tokens": 128
  }'
```

### Completion

```bash
curl http://127.0.0.1:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3-70b",
    "prompt": "Tell me a short story about a sailor.",
    "temperature": 0,
    "max_tokens": 128
  }'
```

If the server returns a valid response, vLLM is ready.

---

## Optional: Run Sanity Check

If you have a local sanity-check script, run:

```bash
python3 /workspace/scripts/sanity_check.py
```

The environment is considered valid when the required checks pass.

---

## Commit the Docker Image

After vLLM, LMCache, PyTorch, and CacheRoute dependencies are installed, you can commit the container as a reusable image:

```bash
sudo docker commit cacheroute-dev cacheroute:vllm0.13-lmcache0.3-pytorch2.9
```

You can then use this image in later CacheRoute experiments.

---

## Common Docker Commands

List containers:

```bash
sudo docker ps -a
```

Start a container:

```bash
sudo docker start <container>
```

Enter a running container:

```bash
sudo docker exec -it <container> bash
```

Remove a container:

```bash
sudo docker rm <container>
```

List images:

```bash
sudo docker images
```

Remove an image:

```bash
sudo docker rmi <image_name>:<tag>
```

Check Python version:

```bash
python --version
```

Check installed package version:

```bash
python3 -m pip show <package_name>
```

---

## Notes

- This document describes the full LLM serving environment. For a lightweight CacheRoute workflow, use the demo scripts in `test/`.
- The default examples assume host networking and a single-machine setup.
- Multi-machine deployment requires updating the service addresses in `core/config.py`.
- Redis-based LMCache configuration is required when validating KDN-based KVCache injection.
- The Instance Resource Dashboard uses port `9202` for the browser fallback and starts the Rust resource agent on `9201` by default.
- Some model paths and image names in the commands should be adjusted according to your local environment.
