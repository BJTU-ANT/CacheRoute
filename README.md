### 260126 大更新(v0.1.0)：重构scheduler，proxy和request部分的知识库维护部分，不再依赖本地yaml预设值。而是实现scheduler启动时抓取KDN服务器中的知识索引，构建知识清单。

(1)KDN_server的/search/text支持按field回传，而不是每次都回传整个结构体<br>
(2)KDN_server支持/snapshot整个知识库状态用于scheduler更新<br>
(3)更新scheduler，摒弃之前的本地yaml构建方式，支持启动初始化从kdn进行snapshot抓取并构建知识清单。<br>
(4)更新knowledge_base，支持sha256至int64映射（注：Faiss是通过INT64检索，而KDN形成的是sha256的str表达格式，所以在snap后需要进行映射。）<br>
(5)优化scheduler_CLI，增强信息维护和交互命令行接口<br>
(6)scheduler新增功能，动态同步KDN知识库状态，采用两阶段增量刷新，第一阶段拉轻量元信息，对于变更项才拉取二阶段<br>

更多日志及其修改详情：https://github.com/BJTU-ANT/CacheRoute/tree/main/doc/blog

---

<img width="1400" height="369" alt="CacheRoute" src="https://github.com/user-attachments/assets/6050e71f-0e37-4cf9-b712-26e11242c9cd" />

CacheRoute是一种基于vLLM和LMCache开发的新型跨LLM系统任务调度平台。考虑到大语言模型的知识密集型业务（如浏览器AI、知识问答AI）涉及大量知识重用，而现有方法主要通过将知识的长文本片段放在问题前作为prompt一同送入模型进行重计算；尽管这种方法能够有效避免模型幻觉提升回复质量，但长知识文本为系统带来了额外的Prefill计算压力，且高重复度的知识片段使得系统产生了大量冗余计算。为此，CacheRoute部署独立服务器保留热门知识的KVCache块，旨在任务需要时直接注入KVCache块进行知识重用。CacheRoute在本地资源池构建了一种任务调度模型，能够动态衡量任务队列情况以及网络和算力的资源负载，为每个任务动态地调整知识注入策略（基于文本的，基于KVCache的）。CacheRoute通过将任务的知识注入成本动态地分摊至网络和计算资源，有效提升了任务性能和系统吞吐量。有关CacheRoute的具体动机和内容见xxx。

---

### 架构
----------------------------------------------------------------------------------------<br>
| [Client] -> [Scheduler] -> [Proxy] -> [Instance] -> [vLLM实例] <- [KDN Server] |<br>
----------------------------------------------------------------------------------------<br>
- Client发起推理任务，发送给Scheduler做全局资源池选择。<br>
- Scheduler收到请求后会解析请求信息并构建Request调度策略，启用面向知识的任务路由。然后基于调度策略生成结果发送给指定的资源池Proxy
- Proxy接收到请求后，根据资源池策略送入具体实例的任务队列等待，同时根据任务模型评估知识注入效率，进而决定任务策略。
- KDN服务器会向instance注入知识所需KVCache，对于满足下发条件的任务proxy将请求移交instance
- instance将请求送入vllm实例并等待回复。设计instance接口主要是为了实现vLLM与Proxy之间的信令交互

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
    
6. 进行准备工作，检查运行环境、预热调度器知识清单。首先，安装requirements.txt内的依赖库`python -m pip install -r requirements.txt`。
7. 测试vLLM服务正常启动，新建容器命令行(注意此处url与启动的vLLM实例的监听端口和监听网卡有关)
    ```
    curl http://127.0.0.1:8000/v1/models
    ```
8. 预热KDN服务器，运行`demo_kdn.py`，启动通过`kdn_api`KDN服务器。启用新终端运行kdn_server下`kdn_register_cli.py`，这是一个封装好的交互式接口，通过送入知识块文本完成文本以及KVCache块的注册，形成知识库。具体方法见`kdn_server/README.md`
9. 在完成KDN预热后，依次启动调度器（调度器在初始化时会向KDN抓取可用知识信息）、代理、客户端和实例demo(在本地IDE调试可以直接用demo_run)
    ```
   cd test
   ./quick_start_docker.sh
   python3 demo_scheduler.py
   python3 demo_proxy.py
   python3 demo_instance.py
   python3 demo_client.py 或 demo_client.py --with-ui（推荐，启动有UI界面的版本，支持自动校验报文）
   ```
   **注意**：如果执行时出现import报错，为容器添加关于项目的工作路径：
   ```
    echo 'export PYTHONPATH=/workspace/llm-stack/CacheRoute' >> ~/.bashrc
    ```
10. 此时scheduler/proxy/instance待完成启动后会发布INFO并等待请求接收，待都启动完毕后，进入client，发现显示<client>，输入http请求即可实现快速示例。
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
  

