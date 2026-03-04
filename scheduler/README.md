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

---

### Workflow
User<br>
 └─> Scheduler (7001)<br>
       &emsp;&emsp;&emsp;&emsp;├─ Request.build_request()   ← 调度策略生效点<br>
       &emsp;&emsp;&emsp;&emsp;├─ Proxy Pool (in-memory)<br>
       &emsp;&emsp;&emsp;&emsp;├─ Knowledge Table (KDN / YAML)<br>
       &emsp;&emsp;&emsp;&emsp;└─ forward_request()<br>
       &emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;      └─> Proxy (900x)<br>
