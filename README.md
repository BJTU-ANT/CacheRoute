# CacheRoute

<img width="1400" height="369" alt="CacheRoute" src="https://github.com/user-attachments/assets/6050e71f-0e37-4cf9-b712-26e11242c9cd" />

[![Version](https://img.shields.io/badge/version-0.1.8-blue)](https://github.com/BJTU-ANT/CacheRoute/releases)
[![License](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/BJTU-ANT/CacheRoute?style=social)](https://github.com/BJTU-ANT/CacheRoute)
[![Built on vLLM](https://img.shields.io/badge/Built%20on-vLLM-6C5CE7?style=flat-square&logo=github&logoColor=white)](https://github.com/vllm-project/vllm)
[![Powered by LMCache](https://img.shields.io/badge/Powered%20by-LMCache-00B894?style=flat-square&logo=github&logoColor=white)](https://github.com/LMCache/LMCache)

CacheRoute is an LLM scheduling framework built on [vLLM](https://github.com/vllm-project/vllm) and [LMCache](https://github.com/LMCache/LMCache) to enable flexible KV cache reuse across LLM systems. It targets knowledge-intensive LLM services, such as browser AI and knowledge QA systems, where many requests repeatedly use the same external knowledge. Existing systems usually prepend long knowledge texts to the user question and send the whole prompt to the model for recomputation. Although this approach helps reduce model hallucination and improve answer quality, it introduces heavy prefill overhead and causes redundant computation when the same knowledge appears across many requests.

CacheRoute addresses this problem by using KDN servers to store KVCache blocks for popular knowledge. For each request, CacheRoute dynamically chooses between text-based injection and KVCache-based injection according to task queues, compute load, and network load. In this way, CacheRoute shifts knowledge injection cost between compute and network resources, improving task latency and system throughput.

## Why CacheRoute?

- **Less redundant prefill computation:** reuse repeated knowledge through KV cache instead of recomputing long prompts.
- **Cross-system KV cache reuse:** share reusable knowledge across LLM systems through KDN servers.
- **Compute-network coordination:** dynamically choose between recomputation and KV cache injection based on real-time resource load.

## Key Features

 - **Feature 1** — Compute-network-aware knowledge injection: CacheRoute dynamically chooses between text recomputation and KVCache reuse. Text injection saves network bandwidth but increases prefill computation, while KVCache injection saves computation but consumes network bandwidth. CacheRoute predicts task cost at the proxy and selects the injection strategy according to current compute and network load.
 - **Feature 2** —  Knowledge-oriented cross-system routing: CacheRoute parses the knowledge requirement before resource-pool scheduling. The scheduler jointly considers knowledge availability, system load, and topology information, and routes requests to the LLM system that can serve the required knowledge more efficiently.
 - **Feature 3** —  KDN-based KV cache management: CacheRoute uses KDN servers to register, store, query, and inject KV cache blocks for reusable knowledge. This enables external knowledge to be reused across LLM systems instead of being repeatedly recomputed.

More logs and update details: https://github.com/BJTU-ANT/CacheRoute/tree/main/doc/blog

---

### Architecture

<p align="center">
  <img width="600" alt="CacheRoute" src="https://github.com/user-attachments/assets/9150a874-4e04-4499-821b-39a850e56db6" />
</p>

- The Client sends an inference request to the Scheduler for global resource-pool selection.<br>
- After receiving the request, the Scheduler parses the request information, builds the request scheduling policy, and enables knowledge-oriented task routing. It then sends the scheduling result to the Proxy of the selected resource pool.
- After receiving the request, the Proxy places it into the task queue of a specific instance according to the resource-pool policy. It also evaluates the knowledge injection efficiency with the task model and selects the task strategy.
- The KDN server injects the required KVCache into the instance. For tasks that meet the release condition, the Proxy forwards the request to the instance.
- The instance sends the request to the vLLM instance and waits for the response. The instance interface is mainly designed to support signaling between vLLM and the Proxy.

Default ports:<br>
| Component | Service Plane | Control Plane |
|---|---:|---:|
| Scheduler | 7001 | 7002 |
| Proxy | 8001 | 8002 |
| Instance | 9001 | - |
| vLLM | 8000 | - |
| KDN Server | 9101 | - |

---

### Requirements

Python version：3.12.11<br>
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

### Quick Start
1. Place the whole CacheRoute project under `/workspace/`.<br>
2. Create a new container that supports vLLM. The required image is `cacheroute:vllm0.13-lmcache3.11-pytorch2.9.1` built from source. If you do not know how to quickly deploy the CacheRoute environment or download models, see `/env/README.md`.<br>
    ```
    sudo docker run --gpus all -it --name CacheRoute --network host --ipc=host --shm-size=64g --ulimit memlock=-1 --ulimit stack=67108864 --memory=0 --memory-swap=0 -p 8000:8000 -v /llm-stack:/workspace/llm-stack cacheroute:vllm0.13-lmcache3.11-pytorch2.9.1 bash
    ```
3. Start and enter the container. This is useful when you need to open multiple container terminals.
    ```
    sudo docker start CacheRoute 
    sudo docker exec -it CacheRoute bash
    ```
   First, start a Redis container as the later KVCache store for `LMcache_connector`.
    ```
    sudo docker run -d --name lmcache-redis --network host redis:7 redis-server --bind 0.0.0.0 --protected-mode no --save "" --appendonly no --maxmemory 200gb --maxmemory-policy allkeys-lru
    ```
4. Configure the required parameters in `core/config.py` according to the actual model download paths. The Scheduler strongly depends on the embedding model, tokenizer, and LLM model.
    ```
    DEFAULT_MODEL:                               Path of the LLM to run
    DEFAULT_MODEL_SHORTNAME:                     Short name of the LLM, used by later vLLM startup commands
    SCHEDULER/PROXY/INSTANCE/KDN_LOG_FILE:       Log output paths of Scheduler/proxy/instance/kdn, <path-to-Cacheroute/log/**>
    EMBEDDING_MODEL:                             Actual path of the locally downloaded embedding model, <path-to-Cacheroute/model/embedder/**>
    DEFAULT_EMBED_MODEL:                         Embedding model name, used to download from Hugging Face when EMBEDDING_MODEL is not configured
    ...
    ```
   There are also many other parameters. See `core/config.py` for detailed descriptions, and see `test/demo_***` for usage examples.<br>
   4.2 To enable KVCache reuse across containers, CacheRoute replaces the unstable `builtin+SEED` key generation method with `sha256_cbor`. However, because of output format mismatch, CacheRoute patches `token_database.py`. Therefore, you need to replace `lmcache/v1/token_database.py` and `lmcache/v1/memory_management.py` in the LMCache source code with `CacheRoute/env/token_database.py` and `CacheRoute/env/memory_management.py`.<br>
   4.3 CacheRoute supports interconnection and scheduling across multi-level inference resource pools. For a quick demo on a single device, this tutorial uses a single-machine setup. It connects `scheduler`, `proxy`, `instance`, and `kdn_server` through loopback addresses and separates modules by ports. For multi-machine experiments, you need to modify the related configurations in `config.py` and `demo`. See `core/README.md` for details.<br>
5. To enable the TTFT predictor in the Proxy, you need to complete offline regression in advance, that is, profiling the model performance under different batch sizes and lengths, and then configure the predictor parameters. See `/instance/TTFT_predictor/README.md` for quickly collecting model regression data. See `proxy/metric` for Proxy predictor regression.
6. Start the vLLM 0.13 + LMCache 3.11 service without PD disaggregation. The following command starts a LLaMA-70B model with TP8. Adjust it according to your needs. Also make sure that `USE_MOCK = False` in `CacheRoute/core/config.py`.
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
     --gpu-memory-utilization 0.75 \
     --dtype auto \
     --max-model-len 4096 \
     --max-num-seqs 8 \
     --max-num-batched-tokens 16384 \
     --kv-offloading-backend lmcache \
     --kv-offloading-size 64\
     --disable-hybrid-kv-cache-manager \
     --kv-cache-metrics
    ```
   (5.1) Note that `LMCACHE_CONFIG_FILE` affects LMCache caching. CacheRoute needs to enable Redis-server-based KV caching. The current `lmcache.yaml` configuration is:
    ```
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
    ```
    
7. Test whether the vLLM service starts correctly. Open a new container terminal and run the following command. Note that the URL depends on the listening port and network interface of the vLLM instance.
    ```
    curl http://127.0.0.1:8000/v1/models
    ```
8. Prepare the environment and warm up the Scheduler knowledge list. First, install the dependencies in `requirements.txt` with `python -m pip install -r requirements.txt`.
9. Enter the `test` directory and start the CacheRoute Scheduler. See `/scheduler/README.md` for parameter options.
    ```
    python3 demo_scheduler.py --cacheroute --kdn-pending-overload-th 8 --kdn-active-overload-th 4 --kdn-queue-ms-overload-th 30 --cacheroute-log-decision 1
    ```
10. Warm up the KDN server. Run `demo_kdn.py` to start the KDN server through `kdn_api`. Then open a new terminal and run `kdn_register_cli.py` under `kdn_server`. This is a packaged interactive interface. It registers text and KVCache blocks by taking knowledge block texts as input, and then builds the knowledge base. See `kdn_server/README.md` for details.
11. After KDN warm-up, start the proxy, client, and instance demos in order. For local IDE debugging, you can directly use `demo_run`. **Note**: The startup order matters. The KDN server and Proxy register with the Scheduler after startup, and then they exchange resource information. The Instance follows the same logic with the Proxy. A wrong startup order may make the resource pool unstable. The safest startup order is `[Scheduler]-[KDN_Server]-[Proxy]-[Instance]`. Also, the default Proxy injection strategy is `text`. After enabling the `iws` strategy, Proxy takes over injection strategy selection. In this case, the `Injection-type` sent by the client will be overwritten and become ineffective.
    ```
    python3 demo_proxy.py --strategy round_robin --injection-strategy iws --ready-release-policy text_bypass
    python3 demo_instance.py --port <default 9001> --host <xxx>
    python3 demo_client.py or demo_client.py --with-ui (recommended, starts the UI version and supports automatic request validation)
    ```
   **Note**: If an import error occurs, add the project path to the container environment:
    ```
    echo 'export PYTHONPATH=/workspace/llm-stack/CacheRoute' >> ~/.bashrc
    ```
    
12. After the Scheduler, Proxy, and Instance start, they will publish INFO logs and wait for requests. After all components are ready, enter the client. When `<client>` is shown, you can input HTTP requests for a quick demo.
   Note that the URL should be the listening address and port of the Scheduler, so that HTTP requests can be parsed and forwarded to the Scheduler. The following gives three local test request demos.<br>
- Chat mode, with streaming or non-streaming output, and with or without RAG.
    ```
    http://127.0.0.1:7001/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "llama3-70b","messages": [{"role": "user", "content": "What is DeepSeek"}],"max_tokens": 64,"stream":"False","RAG":"True"}'
    ```
    ``` 
    http://127.0.0.1:7001/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "llama3-70b","messages": [{"role": "user", "content": "What is DeepSeek"}],"max_tokens": 64,"stream":"True","RAG":"True"}'
    ```
- Completion mode, with or without RAG.
    ```
    http://127.0.0.1:7001/v1/completions -H "Content-Type: application/json" -d '{"model": "llama3-70b","prompt": "What is DeepSeek","max_tokens": 64,"RAG":"True"}'
    ```
    ```
    http://127.0.0.1:7001/v1/completions -H "Content-Type: application/json" -d '{"model": "llama3-70b","prompt": "What is DeepSeek","max_tokens": 64,"RAG":"False"}'
    ```
- Option descriptions:<br>
`model`: required. The actual model path enabled by vLLM.<br>
`message/prompt`: required. Fill it according to the request mode, either chat or completion.<br>
`max_tokens`: optional. The maximum number of generated tokens.<br>
`stream`: optional. Whether to enable streaming responses. Note that completion mode only supports non-streaming responses.<br>
`RAG`: optional. Whether to enable knowledge injection. If set to `False`, the Scheduler will skip knowledge retrieval for this task.
  
Scheduler task scheduling example
<img width="1200" height="559" alt="image" src="https://github.com/user-attachments/assets/320b5058-04b2-4de3-aa3b-aaa714b69982" />

Proxy task scheduling example
<img width="1200" height="288" alt="image" src="https://github.com/user-attachments/assets/bc24230e-0167-469b-9e6a-a7be9f5d26f0" />

Proxy injection strategy selection
<img width="1200" height="746" alt="image" src="https://github.com/user-attachments/assets/930575a6-dba2-465d-aff2-b511099a25a4" />

vLLM + LMCache reuse example
<img width="1200" height="224" alt="image" src="https://github.com/user-attachments/assets/558be19f-c801-4182-b9cd-7daee7fd0a80" />

Client response
<img width="1200" height="374" alt="image" src="https://github.com/user-attachments/assets/5c2c891b-8eeb-4a69-85f9-f7bc588f38bc" />

---

### Current Status (Scheduler / CacheRoute)

The current version supports the following CacheRoute functions on the Scheduler side through `cacheroute`:
- KDN selection based on knowledge coverage and overload filtering.
- Proxy selection based on topology hierarchy, load safety window, and knowledge history preference, using non-weighted lexicographic ordering.
- Policy observation through `/debug/status` and `/debug/strategy`.

Suggested minimum validation commands:
```bash
cd test
python3 demo_scheduler.py --cacheroute
curl -s http://127.0.0.1:7001/debug/status
curl -s http://127.0.0.1:7001/debug/strategy
```




  
