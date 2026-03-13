### Client
发送用户http请求至调度器，并等待调度器返回的流式（非流式）响应。

### 代码结构：
(1) **client.py**:提供接收http请求的cli接口，解析请求附带字段是否合法(配置合法字段见core.config)
。<br>用法：<u>python3 client.py</u>。<br>
&emsp;&emsp;可用两种模式：<br>
&emsp;&emsp;&emsp;&emsp; - chat_completion:对话模式，vllm根据系统提示词和上下文以对话的形式回答用户问题<br>
&emsp;&emsp;&emsp;&emsp; - completion:补全模式，vllm根据用户发送问题接着后面补全最优回复<br>

### 请求示例
示例以环回地址自测为例，实际使用url需要替换为scheduler的{ip_address:port}<br>
(1) client以CLI模式启动对话，并动态显示模型推理回复，支持chat和completion两种对话模式。具体在开启CacheRoute基础上运行`test/demo_client.py`。使用方法：<br>
 - chat模式示例：

```
http://127.0.0.1:7001/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "llama3-70b","messages": [{"role": "user", "content": "What is vllm?"}],"max_tokens": 64,"stream":"True","RAG":"True","Injection_type":"kvcache"}'
```

其中，`injection_type`允许用户强制知识注入模式（text或kvcache），`stream`设置回复是否以流式进行，`RAG`确定是否启用知识注入增强回复。
 - completion模式示例：

```
http://127.0.0.1:7001/v1/completions -H "Content-Type: application/json" -d '{"model": "llama3-70b","prompt": "What is DeepSeek","max_tokens": 64,"RAG":"True"}'
```

 <img width="1200" height="548" alt="image" src="https://github.com/user-attachments/assets/f7d5aff5-4173-496d-83f7-ed8bad431620" />



(2) 并发压力测试器`client/perf_client.py`，用于并发任务包以测试系统性能，支持显示任务的阶段性能以及整体测试平均任务性能,它根据rps等负载要求从指定`workload.json`取任务送入CacheRoute：<br>
  
<img width="553" height="73" alt="image" src="https://github.com/user-attachments/assets/3a6b3b0c-851a-44cf-8f62-d453b926b7c2" />

使用方法：

 - 并发模式：`--base-url`：scheduler服务地址，`--url-path`：vLLM后缀API路径默认“v1/chat/completions”，`--workload-file`：请求任务库的json文件路径，`--request`：发送的总请求数量，`--concurrency`：最大并发请求数量，`--allow-duplicate` 允许在任务集中重复抽取，`--seed`：相同的seed将抽样顺序固定，便于复现。
```
python3 perf_client.py --base-url http://127.0.0.1:7001 --workload-file taskset/workload_nq.json --model llama3-70b --stream true --rag true --injection-type text --max-tokens 64 --temperature 0.8 --top-p 1.0 --requests 20 --concurrency 4 --seed 42
```
 - RPS模式：以预设RPS发送数据包，完成Request个任务结束，统计任务平均性能。`rps`设置RPS模式，`injection-type`：设置任务的知识注入模式，支持‘text’‘kvcache’和‘hybrid’。
```
python3 perf_client.py --mode rps --base-url http://127.0.0.1:7001 --workload-file taskset/workload_nq.json --model llama3-70b --stream true --rag true --injection-type kvcache --requests 30 --rps 0.1 --seed 118
```
<img width="1200" height="1193" alt="image" src="https://github.com/user-attachments/assets/f74b8e51-c11b-408a-b422-021f967766ea" />

