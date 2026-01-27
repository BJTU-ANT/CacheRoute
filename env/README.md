## CacheRoute 容器环境搭建
2026.1.26 v0.1.0版本

### 一、容器基本操作
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
---
### 二、构建新版本的vLLM+LMCache镜像
基础环境：<br>
    system`Ubuntu22.04.5 Jammy`<br>
    docker`Docker version 28.2.2, build 28.2.2-0ubuntu1~22.04.1`<br>
    CUDA_version `13.0`<br> 
    NVIDIA_driver `580.95.05`<br>
---
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
