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
  
<img width="1200" height="73" alt="image" src="https://github.com/user-attachments/assets/3a6b3b0c-851a-44cf-8f62-d453b926b7c2" />

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


(3) KVCache时间建模发送器`client/kv_timing_sender.py`：按RPS发包，并输出KV命中/重算相关统计，便于做注入时间预测建模。<br>

核心输出字段包括：
- 请求ID（若上游未返回则退化为req_index）
- 总长度（`predict_length_tokens`）
- 实际命中长度（按256对齐）
- 剩余重算长度
- KVCache体积估算（`hit_tokens * kv_gb_per_token`）
- queue_wait_ms（proxy `actual_wait_ms`）
- compute_ms（proxy `actual_compute_ms`）
- text_compute_estimate_ms（`proxy.metrics.queue_predictor` 对剩余长度估算）
- lmcache_redis_pull_ms（`compute_ms - text_compute_estimate_ms`）
- total_ms（proxy `actual_total_ms`）

示例：
```
python3 kv_timing_sender.py \
  --base-url http://127.0.0.1:7001 \
  --workload-file taskset/workload_nq.json \
  --model llama3-70b \
  --stream true \
  --rag true \
  --injection-type kvcache \
  --requests 30 \
  --rps 1 \
  --seed 118 \
  --output-jsonl ./out/kv_timing.jsonl \
  --output-csv ./out/kv_timing.csv \
  --enable-scheduler-knowledge-peek true
```

说明：
- 若 workload 每条请求提供 `knowledge_length_tokens`，脚本会按该值计算命中长度；
- 若未提供，脚本默认会先调用 scheduler 的 `/debug/knowledge/peek` 拉取每个 `kid` 的真实 `length`，再计算命中长度；
- 仅当无法从 workload 与 scheduler 都拿到知识长度时，才回退使用 `predict_length_tokens` 估算（会包含任务与首部，精度较低）。
- 命中长度始终以知识长度为上限：`actual_hit_length_tokens <= knowledge_length_tokens`。
<<<<<<< codex/discuss-kvcache-based-time-predictor-model
- 脚本会先输出 `knowledge_length_tokens_raw`（原始解析值），并裁剪得到 `knowledge_length_tokens <= total_length_tokens`，避免知识长度大于总长度。
- 256 对齐规则固定保留：`actual_hit_length_tokens = floor(knowledge_length_tokens / 256) * 256`。
=======
>>>>>>> main
- 输出里会包含 `knowledge_length_source` 和 `knowledge_ids_for_length`，用于排查长度来源是否正确。
