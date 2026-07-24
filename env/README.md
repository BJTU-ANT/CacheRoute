# Environment Setup

This document describes the full CacheRoute environment with CUDA GPUs, vLLM, LMCache, Redis, and the optional Instance Resource Agent/Dashboard.

CacheRoute supports two environment paths:

1. **Existing complete image:** create a new experiment container from an image that already contains PyTorch, vLLM, and LMCache. This is the recommended path for repeated experiments.
2. **Source-build path:** build the provided CUDA base image, install PyTorch, build vLLM and LMCache, and then preserve the resulting environment in a derived image.

Do not mix the two paths. In particular, do not reinstall a different PyTorch build over a working vLLM/LMCache image.

---

## Tested Environment

| Component | Tested version |
|---|---|
| Host OS | Ubuntu 22.04.5 Jammy |
| Docker | 28.2.2 |
| NVIDIA driver | 580.95.05 |
| Container CUDA toolkit | 12.8 |
| PyTorch | 2.9.1 |
| vLLM | 0.13.x |
| LMCache | 0.3.11 |
| Python | 3.12.x |
| Redis | 7 |
| Rust | Stable toolchain, optional |
| Tkinter | `python3.12-tk`, optional |

The host driver may be newer than the CUDA toolkit in the container, but the driver, CUDA, PyTorch, vLLM, and LMCache versions must remain mutually compatible.

Rust and Cargo are required only by `instance/resource_agent`. Tkinter is required only by the desktop dashboard at `instance/resource_dashboard/dashboard_app.py`. The browser dashboard does not require Tkinter or a graphical display.

---

## 1. Prepare the Host

Install Docker Engine first by following the official Docker instructions for the host distribution. Then install and configure the NVIDIA Container Toolkit.

For Ubuntu or Debian:

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends ca-certificates curl gnupg2

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Verify the host driver and container GPU access:

```bash
nvidia-smi
sudo docker run --rm --gpus all \
  nvidia/cuda:12.8.0-base-ubuntu22.04 \
  nvidia-smi
```

---

## 2. Create the Workspace

CacheRoute uses one host directory for source code, models, cache data, logs, and configuration. The examples in this repository use `/llm-stack` on the host and mount it at `/workspace/llm-stack` in the container.

```text
/llm-stack/
├── CacheRoute/
├── vllm/
├── LMCache/
├── models/
├── config/
├── cache/
│   ├── hf/
│   ├── torch/
│   └── lmcache/
├── lmcache-data/
└── logs/
```

Create the workspace and clone the repositories:

```bash
sudo mkdir -p /llm-stack/{models,config,cache/{hf,torch,lmcache},lmcache-data,logs}
sudo chown -R "$USER":"$USER" /llm-stack

cd /llm-stack
git clone https://github.com/AstraNetLab/CacheRoute.git
git clone https://github.com/vllm-project/vllm.git
git clone https://github.com/LMCache/LMCache.git
```

The resulting CacheRoute path is:

```text
Host:      /llm-stack/CacheRoute
Container: /workspace/llm-stack/CacheRoute
```

---

## 3. Choose an Image Path

### Path A: Use an existing complete image

For repeated CacheRoute experiments, use an image that already contains the compatible PyTorch, vLLM, and LMCache stack. The examples below use:

```text
cacheroute:vllm0.13-lmcache3.11-pytorch2.9.1
```

The image tag uses `lmcache3.11` as a compact label; the corresponding LMCache package version is `0.3.11`.

Skip the source-build sections when using this image.

### Path B: Build the provided base image

The repository Dockerfile installs CUDA development dependencies, Python 3.12, Rust/Cargo, and `python3.12-tk`. It does not install the final PyTorch, vLLM, and LMCache stack.

```bash
cd /llm-stack/CacheRoute/env/docker
sudo docker build -t basic-cu128 .
```

After creating a container from `basic-cu128`, follow the source-build sections below.

---

## 4. Create the CacheRoute Container

### 4.1 Headless or browser-dashboard mode

This mode is sufficient for vLLM, LMCache, Redis, the CacheRoute services, and the browser dashboards.

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

`--network host` exposes services directly on the host network, so `-p` port mappings are redundant. If host networking is not used, explicitly publish the required ports, such as `8000`, `7001`, `8202`, and `9202`.

### 4.2 Optional Tkinter desktop-dashboard mode

Tkinter support has two independent requirements:

1. `python3.12-tk` must exist in the image.
2. The container must receive the host X11 display at runtime.

On the host, grant the local root user access to the X server:

```bash
xhost +si:localuser:root
```

Then create the container with the additional display settings:

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
  -e DISPLAY="$DISPLAY" \
  -v /tmp/.X11-unix:/tmp/.X11-unix:rw \
  -v /llm-stack:/workspace/llm-stack \
  -w /workspace/llm-stack \
  cacheroute:vllm0.13-lmcache3.11-pytorch2.9.1 \
  bash
```

Do not hard-code `DISPLAY=:0` in the Dockerfile. The actual display may be `:0`, `:1`, or another value.

When desktop access is no longer needed, revoke the authorization on the host:

```bash
xhost -si:localuser:root
```

If `$DISPLAY` is empty or X11 forwarding is unavailable, use the browser dashboard instead.

---

## 5. Verify the Container

Inside the container, verify the core serving stack:

```bash
python3 --version
python3 -m pip show torch vllm lmcache

python3 - <<'PY'
import torch

print("torch:", torch.__version__)
print("cuda available:", torch.cuda.is_available())
print("cuda runtime:", torch.version.cuda)
print("visible GPUs:", torch.cuda.device_count())
PY
```

Verify the optional Rust resource agent toolchain:

```bash
rustc --version
cargo --version
```

Verify that Tkinter is installed for the active Python interpreter:

```bash
python3 - <<'PY'
import sys
import tkinter

print("python:", sys.executable)
print("tkinter:", tkinter.TkVersion)
PY
```

The import test only verifies the Python/Tk libraries. Test graphical display separately:

```bash
python3 -m tkinter
```

If the import succeeds but the window fails with `no display name` or `cannot open display`, recreate the container with the X11 options in Section 4.2, or use the browser dashboard.

---

## 6. Install CacheRoute Application Dependencies

From the repository root:

```bash
cd /workspace/llm-stack/CacheRoute
python3 -m pip install -U pip
python3 -m pip install -r requirements.txt
python3 -m pip check
```

`requirements.txt` intentionally does not pin PyTorch, vLLM, or LMCache. These packages belong to the serving image and must remain compatible with its CUDA environment.

Optional tools:

```bash
python3 -m pip install modelscope openai
```

---

## 7. Repair an Older or Custom Image

Use this section only when an existing image lacks Rust/Cargo or Tkinter. These changes are stored in the current container writable layer and disappear if the container is deleted unless a new image is created.

### Install Tkinter

```bash
apt-get update
apt-get install -y --no-install-recommends python3.12-tk
rm -rf /var/lib/apt/lists/*

python3 -c "import tkinter; print('Tkinter:', tkinter.TkVersion)"
```

If the active interpreter is Conda Python rather than `/usr/bin/python3.12`, install the matching Conda `tk` package instead.

### Install Rust and Cargo

```bash
apt-get update
apt-get install -y --no-install-recommends curl ca-certificates build-essential pkg-config libssl-dev

export RUSTUP_HOME=/opt/rustup
export CARGO_HOME=/opt/cargo
export PATH="/opt/cargo/bin:$PATH"

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
  sh -s -- -y --profile minimal --default-toolchain stable

rustc --version
cargo --version
```

For a reproducible public environment, prefer updating `env/docker/Dockerfile` or creating a derived Dockerfile instead of relying only on manual container changes.

---

## 8. Source-Build Path Only

Skip this section when using the complete image from Path A.

### 8.1 Install PyTorch for CUDA 12.8

```bash
python3 -m pip install -U pip
python3 -m pip install \
  --index-url https://download.pytorch.org/whl/cu128 \
  torch torchvision torchaudio
```

### 8.2 Build vLLM

```bash
cd /workspace/llm-stack/vllm
git checkout v0.13.0
python3 use_existing_torch.py
python3 -m pip install -U pip setuptools wheel packaging
python3 -m pip install -r requirements/build.txt

export MAX_JOBS=8
python3 -m pip install --no-build-isolation -e .
```

### 8.3 Install LMCache

```bash
cd /workspace/llm-stack/LMCache
python3 -m pip install -e .
python3 -m pip show lmcache
```

Validate the resulting stack before continuing:

```bash
python3 -m pip check
python3 -m pip show torch vllm lmcache
```

---

## 9. Configure the Chat Template

Stable KVCache reuse requires deterministic prompt prefixes. Do not include time-varying fields, such as the current date, in the chat template.

An example tokenizer configuration is provided at:

```text
/workspace/llm-stack/CacheRoute/env/tokenizer_config.json
```

For a model without a suitable chat template, copy a compatible vLLM template into the model's `tokenizer_config.json` before warming the cache.

---

## 10. Start Redis and Configure LMCache

Start Redis on the host network:

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

Create the LMCache configuration:

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

For CacheRoute's cross-container cache-key behavior, follow the LMCache patch instructions in the main README and `kdn_server/README.md`.

---

## 11. Start vLLM with LMCache

The following example starts a LLaMA-70B model with tensor parallelism across eight GPUs. Adjust the paths and resource limits for the actual system.

```bash
export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export MODEL_DIR=/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct
export LMCACHE_CONFIG_FILE=/workspace/llm-stack/config/lmcache_with_redis.yaml
export PYTHONHASHSEED=0
export OMP_NUM_THREADS=8

pkill -f vllm || true
pkill -f api_server || true

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

Verify the server from another terminal:

```bash
curl -sS http://127.0.0.1:8000/v1/models | python3 -m json.tool
```

---

## 12. Run the Resource Agent and Dashboard

Build-check the Rust agent:

```bash
cd /workspace/llm-stack/CacheRoute
cargo check --manifest-path instance/resource_agent/Cargo.toml
```

### Desktop dashboard

The container must have been created with the X11 settings in Section 4.2.

```bash
python3 instance/resource_dashboard/dashboard_app.py \
  --agent-listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

### Browser dashboard

Use this mode in headless environments:

```bash
python3 instance/resource_dashboard/dashboard_server.py \
  --dashboard-listen 0.0.0.0:9202 \
  --agent-listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

Open:

```text
http://127.0.0.1:9202
```

Validate the APIs:

```bash
curl -sS http://127.0.0.1:9202/api/health | python3 -m json.tool
curl -sS http://127.0.0.1:9202/api/snapshot | python3 -m json.tool
curl -sS http://127.0.0.1:9201/healthz
curl -sS http://127.0.0.1:9201/v1/resource/snapshot | python3 -m json.tool
```

The resource dashboard is optional and does not change Scheduler routing, Proxy forwarding, Instance execution, or KDN injection.

---

## 13. Start CacheRoute

Set `USE_MOCK = False` in `core/config.py`, configure the model and embedding paths, and start the components in this order:

```text
Scheduler -> KDN Server -> Proxy -> Instance -> Client
```

From `/workspace/llm-stack/CacheRoute/test`, typical commands are:

```bash
python3 demo_scheduler.py --cacheroute
python3 demo_kdn.py
python3 demo_proxy.py \
  --strategy round_robin \
  --injection-strategy iws \
  --ready-release-policy text_bypass
python3 demo_instance.py --port 9001 --host 127.0.0.1
python3 demo_client.py --with-ui
```

For component-specific parameters, see the READMEs under `scheduler/`, `proxy/`, `instance/`, `kdn_server/`, and `client/`.

---

## 14. Container Lifecycle

Start and enter an existing container:

```bash
sudo docker start cacheroute-main
sudo docker exec -it cacheroute-main bash
```

A container name does not preserve the creation configuration. If a container is deleted and recreated with the same name, its bind mounts, working directory, X11 settings, environment variables, and startup command must be supplied again.

Inspect the effective configuration:

```bash
sudo docker inspect cacheroute-main
sudo docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}' cacheroute-main
```

To preserve a manually assembled environment temporarily:

```bash
sudo docker commit cacheroute-main cacheroute:local-vllm0.13-lmcache0.3.11-pytorch2.9.1
```

`docker commit` is convenient for local snapshots but is not a reproducible build specification. For public releases, preserve system dependencies in a Dockerfile and keep Python application packages in `requirements.txt`.

---

## Notes

- The examples assume a single host and host networking.
- Multi-machine deployment requires updating service addresses in `core/config.py`.
- The browser dashboards are the preferred option for remote or headless servers.
- Tkinter belongs to the image or operating-system package layer, not `requirements.txt`.
- `DISPLAY` and `/tmp/.X11-unix` belong to `docker run`, not the Dockerfile.
- PyTorch, vLLM, and LMCache belong to the serving image and should not be replaced by application dependency installation.
