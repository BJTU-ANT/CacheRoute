### CacheRoute Scheduler

CacheRoute Scheduler 是一个基于 FastAPI 的大语言模型推理调度器。<br>
它位于两级调度的第一级，负责根据选择调度策略，为推理任务选择最优LLM系统。<br>
Scheduler会维护KDN和LLM系统资源池，掌握其可用知识与动态负载。随后基于KDN服务器与Proxy资源池状态对用户推理任务进行调度与转发，实现面向知识的任务路由。<br>
Scheduler默认启动通过7001端口监听业务平面请求，同时它还会拉起7002控制平面来自动监听来自KDN和proxy的信息，负责注册、更新以维护KDN池和proxy池，进而供业务平面策略调用。<br>

---

### Quick Start
快速启动Scheduler：
```
cd CacheRoute/test
python3 demo_scheduler.py --strategy <strategy_name>
```
注意，由于scheduler需要分析任务需求，demo_scheduler中涉及tokenizer/embedder/model的配置信息，具体见：<br>
 - `SCHEDULER_MODEL_PATH`       实际运行模型路径<br>
 - `SCHEDULER_TOKENIZER_MAP`    tokenizer路径<br>
 - `SCHEDULER_EMBEDDING_MODEL`  embedding模型路径<br>

启动Scheduler CLI监视窗口，支持查看、调试或配置资源池等状态信息：
```
cd scheduler
python3 scheduler_cli.py
```
进入后显示简易命令清单：
<img width="1200" height="344" alt="image" src="https://github.com/user-attachments/assets/a63ef61f-b3e6-40e8-b132-6c978dd43f25" />

查看知识状态：
<img width="1200" height="476" alt="image" src="https://github.com/user-attachments/assets/7f1e1d81-c599-4ae2-9e76-83f66030d4fa" />

查看KDN资源池状态：
<img width="1200" height="87" alt="image" src="https://github.com/user-attachments/assets/d02024c0-64ae-4c8b-a639-772003247b3f" />

查看代理池状态：
<img width="1200" height="107" alt="image" src="https://github.com/user-attachments/assets/3ecdcc5f-f238-4443-888a-b8635811254a" />

查看scheduler策略信息：
<img width="1200" height="127" alt="image" src="https://github.com/user-attachments/assets/508b5447-ce8b-49fc-b530-2e176868965b" />

---

### Workflow
User<br>
 └─> Scheduler (7001)<br>
       &emsp;&emsp;&emsp;&emsp;├─ Request.build_request()   ← 调度策略生效点<br>
       &emsp;&emsp;&emsp;&emsp;├─ Proxy Pool (in-memory)<br>
       &emsp;&emsp;&emsp;&emsp;├─ Knowledge Table (KDN / YAML)<br>
       &emsp;&emsp;&emsp;&emsp;└─ forward_request()<br>
       &emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;      └─> Proxy (900x)<br>

---

### CacheRoute 策略阶段总结（Scheduler 侧）

当前 `cacheroute` 已完成以下调度流程（非加权、词典序）：

1. **KDN 选择**：`text_full -> not_overloaded -> kv_cover_len -> load/tie-break`。<br>
2. **Proxy 选择**：`topology_best_group -> load_safe_window -> knowledge_affinity -> load/tie-break`。<br>
3. **观测接口**：`/debug/status` 与 `/debug/strategy` 可用于查看策略、资源池与最近决策快照。<br>

---

### 如何验证策略是否生效

1) 启动 scheduler（可用快捷参数）：
```bash
cd test
python3 test/demo_scheduler.py \
  --cacheroute \
  --kdn-pending-overload-th 8 \
  --kdn-active-overload-th 4 \
  --kdn-queue-ms-overload-th 30 \
  --cacheroute-log-decision 1
```
可选附加参数（便于实验调参）：
- `--kdn-pending-overload-th <int>` KDN为排队任务设置的过载阈值判定，当pending_transfers>阈值时视为过载
- `--kdn-active-overload-th <int>` KDN为活跃任务数设置的过载阈值判定
- `--kdn-queue-ms-overload-th <float>` KDN为队列时延设置的过载阈值判定
- `--cacheroute-log-decision {0|1}` 是否打印每个请求的一行决策日志。

2) 启动 proxy 并注入拓扑 tier（可选，但建议用于验证第二阶段）：
```bash
python3 demo_proxy.py --strategy least_inflight --kdn-links-json '{"kdn_a":{"bandwidth_tier":3,"latency_tier":1}}'
```
3) 如果要让 KDN 上报 runtime 负载（pending/active/queue_ema），建议打开网络模拟：
```bash
python3 demo_kdn.py --network --network-bw-mb-s 125 --network-batch-window-ms 10 --network-fixed-latency-ms 10 --network-efficiency 0.8
```

4) 查看 scheduler 状态（确认策略已加载）：
```bash
curl -s http://127.0.0.1:7001/debug/status | python3 -m json.tool
```
重点检查字段：
- `strategy`：应为 `cacheroute`
- `proxies`：查看 inflight/qps_1m/gpu_util
- `kdns`：查看 items/pending_transfers/active_transfers/network_queue_ms_ema
- `kdn_alive` 与 `kdn_alive_addrs`

5) 查看策略最近决策（确认 CacheRoute 规则在执行）：
```bash
curl -s http://127.0.0.1:7001/debug/strategy | python3 -m json.tool
```
重点检查：
- `strategy`：`cacheroute`
- `strategy_debug.kdn_candidates`
- `strategy_debug.proxy_candidates`
- `strategy_debug.chosen_kdn_id / chosen_proxy_id`
- `strategy_debug.counters`：请求总数、拓扑命中与负载安全过滤统计

6) 观察简洁日志（每请求一行）：
- 默认会输出：`[CacheRoute] req=... kdn=... proxy=... kids=...`
- 若想关闭：`export SCHEDULER_CACHEROUTE_LOG_DECISION=0`

---

### Scheduler 如何维护 KDN 与 Proxy 资源

- Scheduler 控制平面（7002）接收 `register / heartbeat / unregister`。<br>
- Proxy 资源池维护：静态能力 + 动态负载（inflight/qps_1m/gpu_util）。<br>
- KDN 资源池维护：可用节点存活 + 负载摘要（items/qps_1m + meta 扩展位）。<br>
- 调度时读取池内“当前 alive 状态”，不会直接依赖一次性请求数据。<br>
