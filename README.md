### 260303 完善scheduler的proxy资源池信息维护

(1)新增proxy_pool中proxy对控制LLM系统的静态处理能力描述，作为后续调度变量它由proxy注册时上报，在完整的生命周期内保持不变，具体涉及<br>
 - proxy所支持的最大并发任务数 `PROXY_MAX_CAPACITY`<br>
 - proxy管理实例数 `PROXY_INSTANCE_COUNT`<br>
 - proxy中管理的每个实例的KVCache内存大小 `PROXY_KV_MEM_PER_INSTANCE_GB`<br>
 - proxy管理实例池的KV内存大小 `kv_cache_pool_gb`<br>
 - proxy对KV缓存的更新策略 `PROXY_KV_CACHE_UPDATE_POLICY`<br>

(2)支持scheduler对流事件的追踪，scheduler根据会话维护每个proxy正在执行的任务数，进而作为LLM系统负载的评判依据之一。此外，它还结合proxy心跳包和scheduler基于流的自校正来维护资源动态性。具体的，为减少维护inflight所带来的成本，采用由scheduler事件驱动+proxy低频校准的混合维护机制。scheduler收到新的任务请求，流数就加1。只要scheduler对proxy的这次转发stream结束了（不管对端是正常结束、异常、被取消、下游断开），scheduler都认为这个inflight周期结束；这样容易做到不漏减。此外，通过proxy的周期汇报来校准，避免大规模负载偏差。<br>

一些提上日程的工作：<br>
(1)KDN服务器的UI搭建，重点是知识可读性（_TODO. chen_）<br>
(2)instance侧需要搭建一个灵活的资源检索平台(主要是基于vllm平台抓取信息)，使得instance面向proxy暴露动态更新的实例负载信息，便于proxy抓取（_TODO. sihan_）<br>
(3)双inflight对池级业务流状态维护(_TODO. heyao_)<br>
(4)知识清单中可用LLM系统的状态更新<br>

更多日志及其修改详情：https://github.com/BJTU-ANT/CacheRoute/tree/main/doc/blog

---

<img width="1400" height="369" alt="CacheRoute" src="https://github.com/user-attachments/assets/6050e71f-0e37-4cf9-b712-26e11242c9cd" />

CacheRoute是一种基于vLLM和LMCache开发的新型跨LLM系统任务调度平台。考虑到大语言模型的知识密集型业务（如浏览器AI、知识问答AI）涉及大量知识重用，而现有方法主要通过将知识的长文本片段放在问题前作为prompt一同送入模型进行重计算；尽管这种方法能够有效避免模型幻觉提升回复质量，但长知识文本为系统带来了额外的Prefill计算压力，且高重复度的知识片段使得系统产生了大量冗余计算。为此，CacheRoute部署独立服务器保留热门知识的KVCache块，旨在任务需要时直接注入KVCache块进行知识重用。CacheRoute在本地资源池构建了一种任务调度模型，能够动态衡量任务队列情况以及网络和算力的资源负载，为每个任务动态地调整知识注入策略（基于文本的，基于KVCache的）。CacheRoute通过将任务的知识注入成本动态地分摊至网络和计算资源，有效提升了任务性能和系统吞吐量。有关CacheRoute的具体动机和内容见xxx。

---

### 架构
-------------------------------------------------------------------------------------------<br>
| [Client] -> [Scheduler] -> [Proxy] -> [Instance (vLLM-LMCache)] <- [KDN Server] |<br>
-------------------------------------------------------------------------------------------<br>
- Client发起推理任务，发送给Scheduler做全局资源池选择。<br>
- Scheduler收到请求后会解析请求信息并构建Request调度策略，启用面向知识的任务路由。然后基于调度策略生成结果发送给指定的资源池Proxy
- Proxy接收到请求后，根据资源池策略送入具体实例的任务队列等待，同时根据任务模型评估知识注入效率，进而决定任务策略。
- KDN服务器会向instance注入知识所需KVCache，对于满足下发条件的任务proxy将请求移交instance
- instance将请求送入vllm实例并等待回复。设计instance接口主要是为了实现vLLM与Proxy之间的信令交互

默认端口：<br>
 - scheduler `[dp:7001,cp:7002]`<br>
 - proxy `[dp:8001,cp:8002]`<br>
 - instance `[9001]`<br>
 - vLLM `[8000]`<br>
 - KDN server `[9101]`

---

### 需要环境库

Python版本：3.12.11<br>
&emsp;- torch==2.3.1<br>
&emsp;- sentence-transformers~=5.1.2<br>
&emsp;- faiss-cpu==1.13.1<br>
&emsp;- fastapi~=0.124.0<br>
&emsp;- pyyaml~=6.0.3<br>
&emsp;- uvicorn~=0.38.0<br>
&emsp;- matplotlib~=3.10.7<br>
&emsp;- aiohttp~=3.13.2<br>
&emsp;- jupyter_client~=8.6.3<br>
&emsp;- transformers~=4.57.3<br>
&emsp;- requests~=2.32.5<br>
&emsp;- jupyter_client~=8.6.3<br>
&emsp;- pandas~=2.3.3<br>
&emsp;- scikit-learn~=1.7.2<br>
&emsp;- jupyter_client~=8.6.3<br>
&emsp;- scipy~=1.16.3<br>
&emsp;- jupyter_client~=8.6.3<br>
&emsp;- datasets~=4.4.2<br>
&emsp;- numpy~=1.26.4<br>
&emsp;- jupyter_client~=8.6.3<br>
&emsp;- warcio~=1.7.5<br>
&emsp;- bs4~=0.0.2<br>
&emsp;- beautifulsoup4~=4.14.3<br>
&emsp;- tqdm~=4.67.1<br>
&emsp;- Booktype~=1.5<br>
&emsp;- safetensors~=0.7.0<br>
&emsp;- pyzmq~=27.1.0<br>
&emsp;- pydantic~=2.12.5<br>
&emsp;- starlette~=0.50.0<br>
&emsp;- httpx~=0.28.1<br>
&emsp;- setuptools~=78.1.0<br>
&emsp;- huggingface-hub~=0.36.0<br>

---

### 快速开始
1. 在系统内/workspace/下放置整体项目CacheRoute<br>
2. 新建支持vllm的容器，需要镜像`cacheroute:vllm0.13-lmcache3.11-pytorch2.9.1`(源码安装)，如果不知道如何快速部署cacheroute环境和下载模型，见`/env/README.md`<br>
    ```
    sudo docker run --gpus all -it --name CacheRoute --ipc=host --shm-size=64g --ulimit memlock=-1 --ulimit stack=67108864 --memory=0 --memory-swap=0 -p 8000:8000 -v /llm-stack:/workspace/llm-stack cacheroute:vllm0.13-lmcache3.11-pytorch2.9.1 bash
    ```
3. 打开容器(涉及开启多个容器命令行时)
    ```
    sudo docker exec -it CacheRoute bash
    ```
4. 若显示容器未启动，启动容器
    ```
    sudo docker start CacheRoute 
    ```
   先启动一个Redis容器，作为LMcache_connector后续的KVCache store.
    ```
    sudo docker run -d --name lmcache-redis --network container:vllm_lmcache_test redis:7 redis-server --save "" --appendonly no --maxmemory 200gb --maxmemory-policy allkeys-lru
    ```
5. 启动vLLM0.13+LMCache3.11服务(非PD分离)，指令启动的是TP8下运行LLaMA-70B模型，自行根据需求调整，同时确保CacheRoute/core/config.py内 `USE_MOCK = False`
    ```
   export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
   export PYTORCH_ALLOC_CONF=expandable_segments:True
   export MODEL_DIR=/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct
   export LMCACHE_CONFIG_FILE=/workspace/llm-stack/config/lmcache_with_redis.yaml
   export PYTHONHASHSEED=0
   export OMP_NUM_THREADS=8
   
   pkill -f vllm || true
   pkill -f api_server || true
   
   python3 -m vllm.entrypoints.openai.api_server \
     --model "$MODEL_DIR" \
     --served-model-name llama3-70b \
     --host 0.0.0.0 --port 8000 \
     --tensor-parallel-size 8 \
     --gpu-memory-utilization 0.8 \
     --dtype auto \
     --max-model-len 4096 \
     --max-num-seqs 8 \
     --max-num-batched-tokens 8192 \
     --kv-offloading-backend lmcache \
     --kv-offloading-size 64\
     --disable-hybrid-kv-cache-manager \
     --kv-cache-metrics
    ```
   (5.1)注意`LMCACHE_CONFIG_FILE`配置对LMCache缓存的影响，CacheRoute需要开启基于Redis服务器KV缓存，当前配置lmcache.yaml文件为:
    ```
    chunk_size: 256

    local_cpu: true
    max_local_cpu_size: 80.0

    remote_url: "redis://127.0.0.1:6379"
    remote_serde: "cachegen"

    local_disk: null
    max_local_disk_size: 0

    save_decode_cache: false
    cache_policy: "LRU"
    numa_mode: null
    ```
    
6. 测试vLLM服务正常启动，新建容器命令行(注意此处url与启动的vLLM实例的监听端口和监听网卡有关)
    ```
    curl http://127.0.0.1:8000/v1/models
    ```
7. 进行准备工作，检查运行环境、预热调度器知识清单。首先，安装requirements.txt内的依赖库`python -m pip install -r requirements.txt`。
8. 首先启动CacheRoute调度器
    ```
    cd test
    ./quick_start_docker.sh
    python3 demo_scheduler.py --strategy <option,round_robin>
    ```
9. 预热KDN服务器，运行`demo_kdn.py`，启动通过`kdn_api`KDN服务器。启用新终端运行kdn_server下`kdn_register_cli.py`，这是一个封装好的交互式接口，通过送入知识块文本完成文本以及KVCache块的注册，形成知识库。具体方法见`kdn_server/README.md`
10. 在完成KDN预热后，依次启动、代理、客户端和实例demo(在本地IDE调试可以直接用demo_run) **注意**：启动存在先后顺序，KDN，proxy启动会向scheduler注册，随后才会交互资源信息。Instance对proxy同理。错误的执行顺序可能导致资源池的不稳定。最为稳妥的启动顺序为：[Scheduler]-[KDN_Server]-[Proxy]-[Instance]
    ```
    python3 demo_proxy.py --strategy <option,round_robin>
    python3 demo_instance.py --port <default 9001> --host <xxx>
    python3 demo_client.py 或 demo_client.py --with-ui（推荐，启动有UI界面的版本，支持自动校验报文）
    ```
   **注意**：如果执行时出现import报错，为容器添加关于项目的工作路径：
    ```
    echo 'export PYTHONPATH=/workspace/llm-stack/CacheRoute' >> ~/.bashrc
    ```
11. 此时scheduler/proxy/instance待完成启动后会发布INFO并等待请求接收，待都启动完毕后，进入client，发现显示<client>，输入http请求即可实现快速示例。
   注意，此处url应为调度器监听地址与端口，确保http请求解析并发往调度器，此处给出基于本地测试的三个请求demo。<br>
- chat模式(流式与非流式，是否启用RAG)
    ```
    http://127.0.0.1:7001/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "llama3-70b","messages": [{"role": "user", "content": "What is DeepSeek"}],"max_tokens": 64,"stream":"False","RAG":"True"}'
    ```
    ``` 
    http://127.0.0.1:7001/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "llama3-70b","messages": [{"role": "user", "content": "What is DeepSeek"}],"max_tokens": 64,"stream":"True","RAG":"True"}'
    ```
- completion模式（是否启用RAG）
    ```
    http://127.0.0.1:7001/v1/completions -H "Content-Type: application/json" -d '{"model": "llama3-70b","prompt": "What is DeepSeek","max_tokens": 64,"RAG":"True"}'
    ```
    ```
    http://127.0.0.1:7001/v1/completions -H "Content-Type: application/json" -d '{"model": "llama3-70b","prompt": "What is DeepSeek","max_tokens": 64,"RAG":"False"}'
    ```
- 选项说明:<br>
`model`:必选项，vLLM启用模型的实际路径。<br>
`message/prompt`:必选项，根据对话模式填入（chat/completion)<br>
`max_tokens`:可选项，最大生成token数<br>
`stream`:可选项，是否启用流式回复。注意，completion模式只能使用非流式<br>
`RAG`:可选项，是否启用知识注入，False调度器将屏蔽该任务的知识检索
  


  

