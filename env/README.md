## CacheRoute 容器环境搭建
2026.1.26 v0.1.0版本

### 一、容器基本操作
新建容器（多卡）
```commandline
docker run --gpus all -it --rm \
  --ipc=host \
  --shm-size=64g \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -p 8000:8000 \
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
