# Environment Setup

This document describes the environment used to run CacheRoute with vLLM, LMCache, Redis, and CUDA GPUs.

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

The exact versions can be adjusted, but the vLLM, LMCache, CUDA, PyTorch, and driver versions should be kept compatible.

---

## Workspace Layout

CacheRoute assumes a shared workspace for source code, models, cache files, and logs.

A recommended layout is:

```text
/llm-stack/
├── src/                         # vLLM and LMCache source code
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
cd /llm-stack/docker
sudo docker build -t basic-cu128 .
```

The image name can be changed according to your local environment. Make sure the CUDA version in the image matches your driver and PyTorch version.

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
  basic-cu128 bash
```
Useful options:

| Option | Description |
|---|---|
| `--gpus all` | Exposes all host GPUs to the container. |
| `--network host` | Uses host networking, which simplifies multi-service communication. |
| `--ipc=host` | Shares IPC namespace with the host. |
| `--shm-size=64g` | Provides enough shared memory for large-model serving. |
| `--ulimit memlock=-1` | Allows memory locking. |
| `-v /llm-stack:/workspace/llm-stack` | Mounts the workspace into the container. |

Restart and enter the container later:

```bash
sudo docker start cacheroute-dev
sudo docker exec -it cacheroute-dev bash
```

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

Important note:

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
- Some model paths and image names in the commands should be adjusted according to your local environment.


## CacheRoute 容器环境搭建
2026.1.26 v0.1.0版本

### 一、容器基本操作
新建容器（多卡）
```commandline
docker run --gpus all -it \
  --network host \
  --ipc=host \
  --shm-size=64g \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  --memory=0 \
  --memory-swap=0 \
  -v /llm-stack:/workspace/llm-stack \
  basic-cu128:with-pytorch2.9.1 bash
```
`--gpus all` 注入宿主机的所有GPU `-it` 保持终端打开 `--rm` 退出后删除容器本体 `--ipc=host` 共享内存与进程通信 `--shm-size=64g` 设置容器内内存大小 `-ulimit memlock=-1` 允许进程锁定任意大小的内存 `--ulimit stack=67108864` 设置进程最大栈大小 `= 64MB` `-p 8000:8000` 宿主机与docker端口映射 `-v` 外部数据挂载

(1)查看容器
```commandline
sudo docker ps -a
```
(2)删除容器
```commandline
sudo docker rm <container>
```
(3)启动容器
```commandline
sudo docker start <container>
```
(4)进入容器
```commandline
sudo docker exec -it <container> bash
```
(5)查看镜像
```commandline
sudo docker images
```
(6)删除镜像
```commandline
sudo docker rmi <image_name>:<tag>
```
(7)检查环境版本
```commandline
python --version
```
(8)查看进程占用率（后续源码安装vllm时可能卡死，观察）
```commandline
ps -eo pid,ppid,cmd,%cpu,%mem --sort=-%cpu | head -n 30
```
(9)查看系统版本
```commandline
lsb_release -a
```
查看vllm等python库版本
```commandline
pip show xxx
```
---
### 二、构建新版本的vLLM+LMCache镜像

基础环境：<br>
    System_version: `Ubuntu22.04.5 Jammy`<br>
    Docker_version: `Docker version 28.2.2, build 28.2.2-0ubuntu1~22.04.1`<br>
    CUDA_version: `13.0`<br> 
    NVIDIA_driver: `580.95.05`<br>
    Pytorch_version: `2.9.1`<br>

安装docker
```commandline
sudo apt --fix-broken install
sudo apt install docker.io
sudo apt install apt-transport-https ca-certificates curl software-properties-common
```
安装docker调用NVIDIA显卡的工具
```commandline
sudo apt install nvidia-container-runtime
systemctl restart docker
```

挂载工作区在根目录下（取决于大片空闲空间在哪，教程中为根目录/，后续需要大量空间存放模型数据）
```commandline
sudo mkdir -p /llm-stack
mkdir -p /llm-stack/{src,models,cache/{hf,torch,lmcache/local_disk},logs,docker}
```
在Disk目录下准备vllm和LMcache源码（vllm0.13+LMCache0.3.12）
```commandline
cd src
git clone https://github.com/vllm-project/vllm.git
git clone https://github.com/LMCache/LMCache.git
```
检查CUDA版本
```commandline
nvidia-smi
```
构建Dockerfile,放到docker目录下，脚本内容见`CacheRoute/env/docker/Dockerfile`<br>
构建长期开发镜像（只有cuda128还有一些基础库，注意这里是128，显卡驱动版本对应）
```commandline
cd /llm-stack/docker
sudo docker build -t basic-cu128 .
```
利用刚刚的镜像创建新的容器，进一步安装环境（选择-it开启命令行，--gpu设置启用GPU，-net配置网络，-v确认外部挂载，--name 配置容器名）
```commandline
docker run --gpus all -it \
  --ipc=host \
  --shm-size=64g \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -p 8000:8000 \
  -v /llm-stack:/workspace/llm-stack \
  basic-cu128:basic
```


进入容器，安装pytorch
```commandline
python3 -m pip install -U pip
python3 -m pip install --index-url https://download.pytorch.org/whl/cu128 torch torchvision 
torchaudio
```
验证GPU可用（pytorch）
```commandline
python3 - <<'EOF'
import torch
print("torch:", torch.__version__)
print("cuda available:", torch.cuda.is_available())
print("cuda:", torch.version.cuda)
print("n_gpus:", torch.cuda.device_count())
EOF
```
按照requirement要求安装CacheRoute所需要的库
```commandline
pip install --no-cache-dir -r /workspace/requirements.txt
pip check
```
安装一个模型下载依赖
```commandline
pip install modelscope openai -i https://pypi.tuna.tsinghua.edu.cn/simple/
```
下载模型（用model_downloads.py，下载到想存储的位置）
升级pip构建工具
```commandline
python3 -m pip install -U pip setuptools wheel packaging
```
开始安装vllm，目前指向稳定的0.13版本
```commandline
cd vllm
git checkout v0.13.0
python3 use_existing_torch.py
pip install -U pip setuptools wheel
pip install -r requirements/build.txt
export MAX_JOBS=8
pip install --no-build-isolation -e .
```
由于llama模型没有自带chat-template，需要用vllm带的库写一个
```commandline
export VLLM_DIR=/workspace/llm-stack/src/vllm
export TEMPLATE=$VLLM_DIR/examples/tool_chat_template_llama3.2_json.jinja
ls -lah "$TEMPLATE"
export MODEL_DIR=/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct

python3 - <<'EOF'
import json, os
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
！！！但需要注意的是，template可能携带Data日期等内容，这回导致KEYS无法长期存储，随时间而失效。一个最简单的方法是筛查并去除template中的变化内容（如时间），因此需要调整template（26.03.13新增注释），修改后的完整tokenizer_config.json见`env/tokenizer_config.json`

后续的vllm启动命令（前面有GPU worker、内存配置）
```commandline
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
export MODEL_DIR=/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct


python3 -m vllm.entrypoints.openai.api_server \
  --model "$MODEL_DIR" \
  --served-model-name llama3-70b \
  --host 0.0.0.0 --port 8000 \
  --tensor-parallel-size 8 \
  --gpu-memory-utilization 0.84 \
  --dtype auto \
  --max-model-len 4096 \
  --max-num-seqs 8 \
  --max-num-batched-tokens 8192
  ```

  客户端验证
  ```commandline
  curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"llama3-70b",
    "messages":[
      {"role":"system","content":"You are a helpful assistant, use chinese to answer user questions."},
      {"role":"user","content":"用海盗的口吻给我讲一个水手的故事"}
    ],
    "temperature":0,
    "max_tokens":250
  }'

 #completion模式
curl http://127.0.0.1:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"llama3-70b",
    "prompt":"用海盗的口吻给我讲一个水手的故事,要用中文回复",
    "max_tokens":200,
    "temperature":0
  }'
  ```
有回复则证明vllm已经成功安装
开始安装LMCache
```commandline
cd /workspace/llm-stack/src
git clone https://github.com/LMCache/LMCache.git
cd LMCache
python3 -m pip install -e .
```
运行scripts里的sanity_check.py，一路[ok]+checkpassed证明环境没有问题（这里把vllm+lmcache的验证注释掉了）
```commandline
python3 /workspace/scripts/sanity_check.py
```
LMCache本地缓存配置
```commandline
mkdir -p /workspace/llm-stack/lmcache_data
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
导出一版image，作为torch+cu128版本
```commandline
sudo docker commit vllm0.13-lmcache3.11-dev cacheroute:vllm0.13-lmcache3.11-pytorch2.9.1
```
由于docker容器太小，保存成新的镜像后，起一个新的容器（内存不限制）
```commandline
sudo docker run --gpus all -it \
  --name cacheroute \
  --ipc=host \
  --shm-size=64g \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  --memory=0 \
  --memory-swap=0 \
  -p 8000:8000 \
  -v /llm-stack:/workspace/llm-stack \
  cacheroute:vllm0.13-lmcache3.11-pytorch2.9.1 \
  bash
```
vllm+LMCache启动方案
```commandline
export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
export PYTORCH_ALLOC_CONF=expandable_segments:True
export MODEL_DIR=/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct
export LMCACHE_CONFIG_FILE=/workspace/llm-stack/config/lmcache.yaml
export PYTHONHASHSEED=0
export OMP_NUM_THREADS=8

pkill -f vllm || true
pkill -f api_server || true

python3 -m vllm.entrypoints.openai.api_server \
  --model "$MODEL_DIR" \
  --served-model-name llama3-70b \
  --host 0.0.0.0 --port 8000 \
  --tensor-parallel-size 8 \
  --gpu-memory-utilization 0.84 \
  --dtype auto \
  --max-model-len 4096 \
  --max-num-seqs 8 \
  --max-num-batched-tokens 8192 \
  --kv-offloading-backend lmcache \
  --kv-offloading-size 64\
  --disable-hybrid-kv-cache-manager \
  --kv-cache-metrics
```
一个redis工具库
```commandline
sudo apt install redis-tools
```
清空redis服务器
```commandline
sudo docker exec -i lmcache-redis redis-cli FLUSHDB
```
查看redis服务器缓存
```commandline
sudo docker exec -i lmcache-redis redis-cli DBSIZE
```
进入Redis命令行
```commandline
sudo docker exec -it lmcache-redis redis-cli
```
