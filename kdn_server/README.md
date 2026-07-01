# KDN Server

The KDN Server is the knowledge management and KVCache injection component in CacheRoute. It maintains reusable external knowledge, tracks KVCache availability, and injects prepared KVCache blocks into the target LMCache backend when KVCache-based knowledge injection is selected.

In CacheRoute, the KDN Server plays two roles:

- **Knowledge metadata plane:** stores text knowledge blocks, embeddings, lengths, file paths, and KVCache readiness information.
- **KVCache injection plane:** builds, stores, queries, and injects reusable KVCache blocks for knowledge-intensive LLM requests.

This allows the Scheduler and Proxy to make knowledge-aware routing and compute-network-aware injection decisions.

---

## Directory Structure

```text
kdn_server/
├── KV_database/
│   └── <knowledge_id>/
│       ├── blocks/              # dumped KVCache blocks
│       ├── manifest.jsonl        # KVCache block metadata
│       └── run_meta.json         # build-time metadata
├── text_database/
│   ├── blocks/                   # registered text blocks
│   ├── tmp/                      # temporary files
│   └── index.sqlite3             # text knowledge index
├── __init__.py
├── kdn_api.py                    # KDN HTTP service
├── kdn_register_cli.py           # interactive KDN management CLI
├── kv_builder.py                 # KVCache construction
├── kv_injector.py                # KVCache injection into Redis / LMCache backend
├── text_db.py                    # text knowledge database
└── README.md
```
Each knowledge block is identified by a content-based hash ID. Text knowledge and KVCache blocks are stored separately, while the KDN metadata links them together.

---

## API Routes

The KDN Server exposes HTTP APIs for knowledge registration, query, deletion, and metadata synchronization.

```text
POST /knowledge/search/text       Search text knowledge blocks and metadata.
POST /knowledge/register_text     Register a new text knowledge block.
POST /knowledge/delete            Delete a knowledge block.
POST /knowledge/purge_all         Clear the KDN database.
POST /knowledge/snapshot          Export KDN metadata for Scheduler synchronization.
```

Typical metadata fields include:

```text
content
length
rel_path
embedding
embed_dim
kv_ready
kv_rel_dir
kv_dumped_keys
kv_updated_at
embedding_head
```
Example query:

```bash
curl -s http://127.0.0.1:9101/knowledge/search/text \
  -H "Content-Type: application/json" \
  -d '{
    "knowledge_ids": [
      "7a2b0b48a2d9b353c57f13c4bf943c9e3c8a6e2dc7cff2619507f39e0447d7fc"
    ],
    "need_fields": ["embedding", "length"]
  }' | head -c 300
```

---

## Quick Start

### 1. Start the KDN Server

From the CacheRoute root directory:

```bash
python3 kdn_server/kdn_api.py
```

By default, the KDN Server listens on port `9101`. The actual host and port can be configured in `core/config.py`.

### 2. Start the KDN CLI

Open another terminal and run:

```bash
python3 kdn_server/kdn_register_cli.py
```

The CLI provides an interactive interface for text registration, KVCache construction, knowledge query, deletion, database cleanup, and KDN pool status inspection.

<img width="600" height="340" alt="KDN CLI" src="https://github.com/user-attachments/assets/26de41b7-5f89-47dd-8024-8f2bd1cba141" />

---

## Text Knowledge Registration

KDN first registers external knowledge as text blocks. For each text block, KDN generates a hash-based knowledge ID, stores the text content, and records metadata such as length, file path, embedding, and embedding dimension.

You can directly paste a short text into the CLI, or register a file:

```text
:file /path/to/the/file
```

Example:

```text
:file /workspace/llm-stack/KDN_server/prompts/req1.txt
```

After registration, the CLI returns an `[ok]` status and shows the corresponding metadata.

<img src="../.assets/readme_kdn_server_cli.png" width="600" alt="KDN text registration">

---

## KVCache Construction

After text knowledge is registered, KDN can send the text block to the vLLM + LMCache engine to build reusable KVCache blocks.

Before running KVCache construction, make sure the following services are ready:

- vLLM + LMCache engine
- Redis backend used by LMCache
- KDN Server

A typical command is:

```bash
python3 kdn_build_kv.py \
  --txt /file/to/.txt \
  --kv-root /path/for/save/kv_cache \
  --api-url http://127.0.0.1:8000/v1/chat/completions \
  --model llama3-70b \
  --max-tokens 1 \
  --redis-host 127.0.0.1 \
  --redis-port 6379 \
  --flushdb
```

A single knowledge block may be split into multiple KVCache chunks. KDN stores the dumped KVCache blocks under a directory named by the corresponding knowledge ID.

```text
KV_database/
└── <knowledge_id>/
    ├── blocks/
    ├── manifest.jsonl
    └── run_meta.json
```

<img src="../.assets/readme_kdn_server_kvdump.png" width="600" alt="KDN KVCache dump">

---

## CLI Commands

The KDN CLI wraps the main KDN maintenance operations.

### Register text

```text
:file /workspace/llm-stack/KDN_server/prompts/req1.txt
```

You can also paste text directly into the CLI.

### Build KVCache for a registered knowledge ID

```text
:buildkv 7a2b0b48a2d9b353c57f13c4bf943c9e3c8a6e2dc7cff2619507f39e0447d7fc \
  --api-url http://127.0.0.1:8000/v1/chat/completions \
  --model llama3-70b \
  --max-tokens 1
```

### Register a file and build KVCache

This is the most common workflow. It registers the text file and builds the corresponding KVCache in one command.

```text
:buildkv_file /workspace/llm-stack/KDN_server/prompts/req2.txt \
  --api-url http://127.0.0.1:8000/v1/chat/completions \
  --model llama3-70b
```

You can add `--flushdb` when needed, but use it carefully because it clears the Redis cache.

<img src="../.assets/readme_kdn_server_cli_2.png" width="600" alt="KDN buildkv file">

### Query knowledge status

```text
:status 7a2b0b48a2d9b353c57f13c4bf943c9e3c8a6e2dc7cff2619507f39e0447d7fc
```

<img src="../.assets/readme_kdn_server_cli_status.png" width="600" alt="KDN status">

### Delete a knowledge block

```text
:delete 30f75ee46371ecb883e24fdf2917d9e0d853961faf01ef3052582d097f6c795d
```

<img src="../.assets/readme_kdn_server_cli_delete.png" width="600" alt="KDN delete">

### Clear the KDN database

```text
:purge
```

To keep KVCache files and only clear text metadata:

```text
:purge --no-kv
```

<img src="../.assets/readme_kdn_server_cli_purge.png" width="600" alt="KDN purge">

### View KDN pool status

```text
:pool
```

You can limit the number of displayed samples:

```text
:pool --sample-limit 5
```

<img width="600" height="260" alt="KDN pool status" src="https://github.com/user-attachments/assets/eadbd610-4424-4e5e-86ff-dd8dfb9fea2b" />

---

## KVCache Injection

KDN can inject prepared KVCache blocks into the Redis backend used by LMCache. This allows the target LLM instance to reuse the injected KVCache blocks during later inference.

A standalone injection command is:

```bash
python3 kv_injector.py \
  --kv-dir /path/save/kvcache \
  --redis-host 127.0.0.1 \
  --redis-port 6379
```

This command is mainly used for functional validation and debugging. In the full CacheRoute workflow, KVCache injection is triggered by the KDN matching and scheduling process.

---

## Network Path Debugging

In local experiments, the Instance control plane may pass `127.0.0.1` as the Redis host. In that case, KDN connects to Redis through the loopback interface. To test cross-machine or cross-NIC behavior, KDN supports host rewriting.

### Rewrite loopback Redis host

```bash
export KDN_REDIS_REWRITE_ENABLE=1
export KDN_REWRITE_LOOPBACK_TO=172.18.0.169
```

When the requested Redis host is `127.0.0.1`, `localhost`, or `::1`, KDN rewrites it to `172.18.0.169`.

### Force a Redis host

```bash
export KDN_REDIS_REWRITE_ENABLE=1
export KDN_FORCE_REDIS_HOST=172.18.0.169
```

With this setting, KDN always connects to the specified Redis host, regardless of the host passed by the upstream control plane.

KDN logs print both `request_host` and `resolved_host` to help verify whether traffic goes through the expected network path.

By default:

```bash
export KDN_REDIS_REWRITE_ENABLE=0
```

or leaving it unset disables host rewriting.

---

## Network Transfer Simulation

When `KDN_NETWORK_ENABLE=1`, KDN enables a simple network transfer simulator for KVCache injection.

The current simulator uses a single-link serial service model:

- only one knowledge transfer task is served at a time;
- later transfer tasks wait in a pending queue;
- acknowledgements are delayed according to the estimated network latency.

The default parameters can be configured through `core/config.py` with the `KDN_NETWORK_*` options. They can also be overridden by environment variables with the same names.

This simulator is useful for validating compute-network-aware injection decisions in CacheRoute.

---

## End-to-End Knowledge Injection Workflow

A complete external knowledge injection workflow contains the following steps.

### 1. Start the KDN Server and CLI

```bash
python3 test/demo_kdn.py
python3 kdn_server/kdn_register_cli.py
```

### 2. Start vLLM + LMCache + Redis

Follow the full deployment guide in the main `README.md`.

### 3. Register text and build KVCache

In the KDN CLI, run:

```text
:buildkv_file /workspace/llm-stack/CacheRoute/kdn_server/test1.txt \
  --api-url http://127.0.0.1:8000/v1/chat/completions \
  --model llama3-70b
```

<img width="600" height="107" alt="KDN buildkv example" src="https://github.com/user-attachments/assets/526aea05-18af-405d-bc0b-355f39c1a97e" />

### 4. Clear Redis and restart the model if needed

For a clean validation, restart the model and run `FLUSHDB` on the Redis backend.

### 5. Inject the prepared KVCache

```bash
python3 kv_injector.py \
  --kv-dir /workspace/llm-stack/CacheRoute/kdn_server/KV_database/a4da9fe548b2b2d66bb5cd1dae29f03a4c0c0eef88fe964757754cad878cc725 \
  --redis-host 127.0.0.1 \
  --redis-port 6379
```

<img width="600" height="138" alt="KVCache injection example" src="https://github.com/user-attachments/assets/372905dc-0201-4108-9fae-f916db5ae997" />

### 6. Verify KVCache reuse

Run the reuse test script:

```bash
python3 test_kv_injector_reuse.py
```

If the injection succeeds, the instance should reuse the injected KVCache blocks through LMCache.

<img width="600" height="60" alt="KVCache reuse test" src="https://github.com/user-attachments/assets/d02b5d2d-1950-4374-a9da-3483929900bc" />

---

## Batch Knowledge Registration

For larger experiments, KDN provides a batch registration script:

```text
kdn_server/util/batch_register_kdn.py
```

The script uses a manifest file to register multiple knowledge documents and optionally build their KVCache blocks.

Example:

```bash
python3 batch_register_kdn.py \
  --manifest knowledge_manifest_nq.json \
  --base-url http://127.0.0.1:9101 \
  --api-url http://127.0.0.1:8000/v1/chat/completions \
  --model llama3-70b \
  --redis-host 127.0.0.1 \
  --redis-port 6379 \
  --redis-db 0 \
  --count all \
  --flushdb
```

Main arguments:

| Argument | Description |
|---|---|
| `--manifest` | Path to the knowledge manifest JSON file. |
| `--base-url` | KDN Server URL. |
| `--api-url` | vLLM OpenAI-compatible API URL. |
| `--model` | Model name served by vLLM. |
| `--redis-host` | Redis host used by LMCache. |
| `--redis-port` | Redis port. |
| `--redis-db` | Redis database index. Default is `0`. |
| `--count` | Number of knowledge items to register. Use `all` for the full manifest. |
| `--flushdb` | Clear Redis before registration. Use carefully. |

The batch script is useful for preparing knowledge-intensive workloads used in CacheRoute experiments.

<img width="600" height="202" alt="Batch KDN registration" src="https://github.com/user-attachments/assets/2bbbca78-93f2-4b08-aab5-268e413580a9" />

---

## Notes

- KDN is currently an experimental component used to validate knowledge-oriented routing and compute-network-aware knowledge injection in CacheRoute.
- The standalone `kv_injector.py` path is mainly for debugging. In the full CacheRoute workflow, KVCache injection should be triggered by the scheduling and KDN matching process.
- Some paths and model names in the examples should be adjusted according to your local deployment.
- For full deployment with vLLM, LMCache, Redis, Scheduler, Proxy, and Instance, see the main `README.md`.

### KDN服务器
数据结构：<br>
kdn_server/<br>
&emsp;| KV_database/<br>
&emsp;| &emsp;| kid_ID/<br>
&emsp;| &emsp;| &emsp;|blocks/(dumps)<br>
&emsp;| &emsp;| &emsp;|manifest.jsonl<br>
&emsp;| text_database/<br>
&emsp;| &emsp;| blocks/(txt)<br>
&emsp;| &emsp;| tmp/<br>
&emsp;| &emsp;| index.sqlite3<br>
&emsp;| __init__.py<br>
&emsp;| kdn_api.py<br>
&emsp;| kdn_register_cli.py<br>
&emsp;| kv_builder.py<br>
&emsp;| kv_injector.py<br>
&emsp;| text_db.py<br>
&emsp;| README.md<br>

**KDN 服务器的POST路由**
```
@/knowledge/search/text: search text block in KDN, input: KEYS, output: text, length, embedding...
@/knowledge/register_text: register text block in KDN, input: text, output: data struct for text.
@/knowledge/delete: delete block in KDN, input KEYS
@/knowledge/purge_all: clean up KDN database
@/knowledge/snapshot: update list for scheduler
 ```

**1.1 知识文本注册**<br>
KDN对知识的注册分两步，第一步是对文本块知识的注册，KDN会基于文本内容生产hash索引命名，并构建存储单元结构数据。封装的接口位于`util/kdn_register_cli.py`内。执行该Python文件前首先需要确保KDN服务器启动（即`kdn_api.py`)
```
python3 kdn_server/kdn_api.py
python3 util/kdn_register_cli.py
```
会进入命令行窗口<br>
 <img src="../.assets/readme_kdn_server_cli.png" width="600" alt="client chat completion 示例"><br>
支持直接输入小于4k的命令行文本进行知识注册，也支持对于长文件给予文件路径的知识块注册
```
 :file /path/to/the/file
```
会得到[ok]状态提示，显示注册结果。结构体包含[hash ID, length, file_path, embedding, embedding_dim]

**1.2 知识KVCache注册**<br>
KDN会使用已经注册知识的文本，送入模型生成KVCache，并落盘到KDN服务器本地。封装的接口位于`util/kdn_build_kv.py`，它基于`kv_builder`，支持将指定路径的文本送入模型生成KVCache并落盘到具体的路径下。命令结构为
```
在运行kdn_build_kv前，确保vLLM+LMCache引擎，KDN服务器和Redis服务器已经正常启动，具体命令见CacheRoute/README.md
python3 kdn_build_kv.py --txt /file/to/.txt --kv-root /path/for/save/kv_cache --api-url vLLM_url --model model_name --max tokens 1 --redis-host ip --redis-port 6379 --flushdb
```
由于一个文本块的KVCache会分为多片。KDN对一个知识块KVCache的存储放在一个目录内，并用其hash ID命名。内设blocks目录存储KVCache分片，并有manifest.jsonl和run_meta.json描述片段信息。<br>
 <img src="../.assets/readme_kdn_server_kvdump.png" width="600" alt="client chat completion 示例"><br>

**1.3 知识KVCache注入**<br>
基于`kdn_server/kdn_injector.py`实现向Redis服务器的KVCache注入。具体指令为
```
python3 kv_injector.py --kv-dir /path/save/kvcache --redis-host ip --redis-port 6379
```
这个方法只是验证功能有效性，后续这个方法会封装在KDN匹配服务的流程内，无需自行调用。

**1.3.1 网络路径调试（让KDN走网卡访问Instance Redis）**<br>
默认情况下，Instance 控制面可能把 `redis_host` 传成 `127.0.0.1`，此时 KDN 会在本机回环访问 Redis。为了模拟跨网，可在 KDN 进程设置：
```
export KDN_REDIS_REWRITE_ENABLE=1
export KDN_REWRITE_LOOPBACK_TO=172.18.0.169
```
含义：仅当请求里的 `redis_host` 是 `127.0.0.1/localhost/::1` 时，改写为 `172.18.0.169`。<br>
也可使用强制覆盖：
```
export KDN_REDIS_REWRITE_ENABLE=1
export KDN_FORCE_REDIS_HOST=172.18.0.169
```
这样不管上游传什么 host，都会让 KDN 走该网卡地址连接 Redis。KDN 日志会打印 request_host 与 resolved_host 便于确认。
默认 `KDN_REDIS_REWRITE_ENABLE=0`（或不设置），此时不会改写任何地址，不影响原有 KVCache 注入路径。

补充：当开启 `KDN_NETWORK_ENABLE=1` 时，KDN 网络模拟器当前采用**单链路串行服务**模型（单服务台排队）：
- 同一时刻仅服务一个知识传输任务
- 后续任务进入 pending 队列等待
- ack 仍按估算网络时延延后返回
上述参数可在 `core/config.py` 中配置默认值（`KDN_NETWORK_*`），并可通过同名环境变量覆盖。

**1.4 知识块信息查询**<br>
KDN_api对外暴露need_field接口，可以根据需求请求对应属性信息，目前开放的属性有：`content`,`length`,`rel_path`,`embedding`,`embed_dim`,`kv_ready`,`kv_rel_dir`,`kv_dumped_keys`,`kv_updated_at`,`embedding_head`，具体例如：
```
curl -s http://127.0.0.1:9101/knowledge/search/text -H "Content-Type: application/json" -d '{"knowledge_ids":["7a2b0b48a2d9b353c57f13c4bf943c9e3c8a6e2dc7cff2619507f39e0447d7fc"],"need_fields":["embedding","length"]}' | head -c 300
```

### **260123 KDN_CLI大更新**<br>
kdn_register_cli对所有KDN数据维护接口进行封装，支持文本注册，KV注册，知识块查询，知识块删除和数据库删除，具体启动方式为`kdn_server/kdn_register_cli.py`，启动后的交互界面如下，脚本内也对使用方法做了详细说明（支持非交互式）。<br>
<img width="600" height="340" alt="image" src="https://github.com/user-attachments/assets/26de41b7-5f89-47dd-8024-8f2bd1cba141" /><br>
(1) 文本注册:<br>
```
:file /workspace/llm-stack/KDN_server/prompts/req1.txt
也可以直接复制文字回车
``` 
(2) KV注册（注意Kids对应实际ID）:<br>
```
:buildkv 7a2b0b48a2d9b353c57f13c4bf943c9e3c8a6e2dc7cff2619507f39e0447d7fc --api-url http://127.0.0.1:8000/v1/chat/completions --model llama3-70b --max-tokens 1
```
(3) 最常用：文件 -> 注册 -> 构建 KV（最常用，后面可加 --flushdb，会清除Redis缓存，慎重）<br>
```
:buildkv_file /workspace/llm-stack/KDN_server/prompts/req2.txt --api-url http://127.0.0.1:8000/v1/chat/completions --model llama3-70b
```
<img src="../.assets/readme_kdn_server_cli_2.png" width="600" alt="client chat completion 示例"><br>
(4) 支持查询知识块
```
:status 7a2b0b48a2d9b353c57f13c4bf943c9e3c8a6e2dc7cff2619507f39e0447d7fc
```
<img src="../.assets/readme_kdn_server_cli_status.png" width="600" alt="client chat completion 示例"><br>
(5)支持删除已有知识块
```
:delete 30f75ee46371ecb883e24fdf2917d9e0d853961faf01ef3052582d097f6c795d
```
<img src="../.assets/readme_kdn_server_cli_delete.png" width="600" alt="client chat completion 示例"><br>
(6)支持清空数据库，默认文本和KV一起清除
```
:purge [--no-kv]
```
<img src="../.assets/readme_kdn_server_cli_purge.png" width="600" alt="client chat completion 示例"><br>
(7)支持查看KDN资源池状态，展示可用知识条目等状态信息
```
:pool [--sample-limit N]
```
<img width="600" height="220" alt="image" src="https://github.com/user-attachments/assets/eadbd610-4424-4e5e-86ff-dd8dfb9fea2b" />


### 一次完整的外部知识注入流程：<br>
 - 通过`demo_kdn.py`开启KDN服务器，并开启`kdn_register_cli.py`打开KDN交互CLI。
 - 开启llm+LMCache+redis组合，见主页README.md
 - 在CLI内执行buildkv_file指令，指定文本路径和模型url，完成知识在kdn_server内的预热，例如：
```
:buildkv_file /workspace/llm-stack/CacheRoute/kdn_server/test1.txt --api-url http://127.0.0.1:8000/v1/chat/completions --model llama3-70b
```
<img width="600" height="107" alt="image" src="https://github.com/user-attachments/assets/526aea05-18af-405d-bc0b-355f39c1a97e" />
 
 - 重启模型，为Redis服务器执行`FLUSHDB`清空缓存
 - 使用`kv_injector.py`注入刚刚准备好的知识条目到Redis，例如：
```
python3 kv_injector.py --kv-dir /workspace/llm-stack/CacheRoute/kdn_server/KV_database/a4da9fe548b2b2d66bb5cd1dae29f03a4c0c0eef88fe964757754cad878cc725 --redis-host 127.0.0.1 --redis-port 6379
```
<img width="600" height="168" alt="image" src="https://github.com/user-attachments/assets/372905dc-0201-4108-9fae-f916db5ae997" />

 - 执行`test_kv_injector_reuse.py`，观察到被重用
<img width="600" height="68" alt="image" src="https://github.com/user-attachments/assets/d02b5d2d-1950-4374-a9da-3483929900bc" />

### 批量注册知识脚本：<br>
提供一个快捷化脚本`kdn_server/util/batch_register_kdn.py`，便于KDN的批量知识注册工作。需要：`kdn_server/util/knowledge_manifest.json`,`data/CacheRoute_dataset/knowledge_document`(支持扩充,仅需在使用时更新knowledge_manifest并对其argument即可)
```
python3 batch_register_kdn.py --manifest knowledge_manifest_nq.json --base-url http://127.0.0.1:9101 --api-url http://127.0.0.1:8000/v1/chat/completions --model llama3-70b --redis-host 127.0.0.1 --redis-port 6379 --redis-db 0 --count all --flushdb
```
其中，`--manifest`指定json文件的路径，`--base-url`指定KDN启用的服务器监听URL，`--api-url`指定vLLM服务监听URL，`--model`指定模型名，`--redis-port`指定redis容器的监听端口，`--redis-db`指定redis仓库，注意为设置默认为0号仓库，`--count`指定注入的条数，可选all，或任意想注入的条目数量，`--flushdb`建议开启但会清空Redis知识库，为防止KVCache连续注册所导致的粘黏。
<img width="600" height="202" alt="image" src="https://github.com/user-attachments/assets/2bbbca78-93f2-4b08-aab5-268e413580a9" />
