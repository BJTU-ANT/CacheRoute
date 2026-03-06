### 260306 大更新（v0.1.4）实现基于文本和基于KVCache注入的全流程行为打通

(1)实现基于KVCache的知识注入，当Injection_type=kvcache时，prepare work仍然拉文本并拼接，但会额外通知选中的Instance将这批knowledge_id的KVCache注入到该Instance本地的redis。Instance完成后ACK给Proxy，Proxy待确认ACK后，任务才进入Ready队列。<br>
(1.1)实现proxy.manager对知识需求的状态分类处理，对每个List_ID，查询相应信息并归类为kv_ready，text_only或miss。在注入到prompt是优先将kv_ready的放在前面，text放在后面。相应的日志会反馈给scheduler，用于上层知识维护的决策。注意，如果kid未build，kdn只把命中的发给Instance，告知miss。然后由Instance反馈给proxy，进而反馈给scheduler miss 情况。因为对于KDN面向Instance和proxy的业务面，下游无权改变上游维护知识的存储结构（考虑到存储策略问题）。<br>
(1.2)实现KVCache注入通信链路，proxy当Injection_type=kvcache时，先做text分类与注入，再等Instance/KDN的ACK，最后进入ready。针对kv_ready的kid发起KV注入请求。搭建proxy-Instance-kdn_server子链路。<br>
(1.3)实现具体KVCache注入行为嵌入，统一消息格式以触发重用。<br>

涉及新增文件：<br>
`instance/kv_service.py`<br>
`instance/control_plane.py`<br>

涉及修改文件:<br>
`proxy/queue/task.py`<br>
`proxy/queue/manager.py`<br>
`proxy/queue/knowledge.py`<br>
`instance/instance_api.py`<br>
`core/config.py`
`kdn_server/kdn_api.py`<br>

一些提上日程的工作：<br>
(1)KDN服务器的UI搭建，重点是知识可读性（_TODO. chen_）<br>
(2)instance侧需要搭建一个灵活的资源检索平台(主要是基于vllm平台抓取信息)，使得instance面向proxy暴露动态更新的实例负载信息，便于proxy抓取（_TODO. sihan_）<br>
(3)双inflight对池级业务流状态维护(_TODO. heyao_)<br>
(4)知识清单中可用LLM系统的状态更新<br>
(5)proxy与Instance的任务并行处理问题<br>

维护者：heyao

---

### 260305 构建Proxy内Prepare+Ready双任务队列结构

(1)串通基于文本的知识注入和基于KVCache的知识注入，添加Injection_type变量来标记任务注入策略，在scheduler build_request过程中默认赋值text，后续待proxy结合实际资源进行更新（text or kvcache）。<br>
(2)引入队列骨架，proxy主handler不再直接调用forward_request(...)，而是决策完成后把任务交给队列模块。队列模块当前仍然“同步drain”（立即出队并转发），所以对外表现不变（待后续开发更多队列行为）。<br>
(3)释放Proxy功能，将知识注入函数移植到外部queue内，仅保留处理request、策略执行、送入队列以及接收队列结果的流水线工作。Proxy保持对外功能不变。引入per-instance的`prepare/ready`双队列，并把知识注入从proxy handler迁到 prepare-queue worker。<br>
 - prepare-queue worker仅负责知识注入的任务准备工作，然后把注入完成的任务丢进ready_q。目前仅支持Injection_type =="text"的知识注入。<br>
 - ready-queue worker：负责真正forward_request到instance，并通过task的response_queue把输出回传给handler。<br>

涉及新增文件：<br>
`proxy/queue/__init__.py`<br>
`proxy/queue/task.py`<br>
`proxy/queue/manager.py`<br>
`proxy/queue/knowledge.py`<br>
`proxy/queue/instance_queues.py`<br>

涉及修改文件:<br>
`core/request.py`<br>
`scheduler/scheduler.py`<br>
`proxy/proxy.py`<br>

维护者：heyao

---

### 260304 完善scheduler与proxy的日志输出

(1)优化scheduler输出显示，避免被心跳包淹没，按周期统计资源池信息做数据整理简报，并输出到log/scheduler/scheduler.log中，而非显示在命令行（命令行仅有极少数重要信息）。<br>
(2)优化proxy输出显示，屏蔽海量心跳包，转而进行周期性统计。<br>

涉及新增文件：<br>
`scheduler/resource/hb_log.py`<br>
`proxy/resource/hb_log.py`<br>

涉及修改文件:<br>
`core/config.py`<br>
`scheduler/scheduler.py`<br>
`scheduler/resource/control_plane.py`<br>
`scheduler/knowledge/kdn_sync.py`<br>
`proxy/proxy.py`<br>

维护者：heyao

---

### 260303 完善scheduler的proxy资源池信息维护

(1)新增proxy_pool中proxy对控制LLM系统的静态处理能力描述，作为后续调度变量它由proxy注册时上报，在完整的生命周期内保持不变，具体涉及<br>
 - proxy所支持的最大并发任务数 `PROXY_MAX_CAPACITY`<br>
 - proxy管理实例数 `PROXY_INSTANCE_COUNT`<br>
 - proxy中管理的每个实例的KVCache内存大小 `PROXY_KV_MEM_PER_INSTANCE_GB`<br>
 - proxy管理实例池的KV内存大小 `kv_cache_pool_gb`<br>
 - proxy对KV缓存的更新策略 `PROXY_KV_CACHE_UPDATE_POLICY`<br>
 
(2)支持scheduler对流事件的追踪，scheduler根据会话维护每个proxy正在执行的任务数，进而作为LLM系统负载的评判依据之一。此外，它还结合proxy心跳包和scheduler基于流的自校正来维护资源动态性。具体的，为减少维护inflight所带来的成本，采用由scheduler事件驱动+proxy低频校准的混合维护机制。scheduler收到新的任务请求，流数就加1。只要scheduler对proxy的这次转发stream结束了（不管对端是正常结束、异常、被取消、下游断开），scheduler都认为这个inflight周期结束；这样容易做到不漏减。此外，通过proxy的周期汇报来校准，避免大规模负载偏差。<br>

涉及修改文件:<br>
`core/request.py`<br>
`core/config.py`<br>
`scheduler/scheduler.py`<br>
`scheduler/scheduler_cli.py`<br>
`scheduler/resource/control_plane.py`<br>
`scheduler/resource/proxy_pool.py`<br>
`proxy/proxy.py`<br>
`proxy/sclient/scheduler_client.py`<br>

维护者：heyao

---

### 260302 Scheduler显示优化，KDN+Proxy调度策略集成

(1)完善scheduler_cli的status查询输出，支持查看kdn资源池状态<br>
(2)优化kdn的知识更新（即`kdn_refresh_once()`函数），现在在old_table更新，可能在并发refresh时造成混乱。因此在更新时先新建一个new_table，并发更新都集中在new_table上，待完毕后统一swap old_table。<br>
(3)将KDN选择策略集成到scheduler的strategy统一策略中，现在scheduler在选择时仅在handle_client()送入proxy池和proxy选择策略，并在request.py执行时确定proxy。kdn则在外面通过一个外挂简单循环实现，没有集成至统一scheduler配置的入口策略中。通过集成，使得KDN选择策略一同集成进scheduler/strategy内<br>
(4)优化kdn_refresh，在KDN注册成功后立即触发一次refresh，而不是等待周期更新。<br>

涉及修改文件:<br>
`core/request.py`<br>
`scheduler/scheduler.py`<br>
`scheduler/scheduler_cli.py`<br>
`scheduler/resource/control_plane.py`<br>
`scheduler/knowledge/kdn_sync.py`<br>
`scheduler/strategy/base.py`<br>
`scheduler/strategy/round_robin.py`<br>
`store/knowledge_base.py`<br>

维护者：heyao

---

### 260202 Proxy、Scheduler池资源结构优化

(1)支持proxy的策略接入Instance池，实现基于池的proxy策略选择，而非默认<br>
(2)优化proxy，在初始化时支持加载策略，不再依赖Scheduler中build_request赋值<br>
(3)更新Scheduler获取知识清单的方式，为其构建KDN池，并在初始化时控制平面构建，由KDN服务器在启动时主动向Scheduler的kdn_pool注册，随后触发snapshot拉取知识清单<br>
(4)CacheRoute中资源获取关系与启动顺序更新：
  ```
  1. Scheduler启动->维护构建proxy_pool和kdn_pool，构建控制平面监听端口（7002），处理来自proxy和kdn的注册、心跳包和注销
  2. KDN && Proxy启动->向Scheduler的控制平面发起注册请求，随后向Scheduler上报资源情况，后续执行个性化资源维护
  3. Instance启动->绑定具体vllm实例，探测资源，向本地proxy注册并上报负载情况
  ```
涉及修改文件:<br>
`core/config.py`<br>
`scheduler/scheduler.py`<br>
`scheduler/resource/control_plane.py`<br>
`scheduler/knowledge/kdn_sync.py`<br>
`scheduler/resource/kdn_pool.py`<br>
`kdn_server/sclient/scheduler_client.py`<br>
`proxy/proxy_cli.py`<br>
`proxy/README.md`<br>
`test/demo_kdn.py`<br>
`README.md`<br>

涉及新增文件:<br>
`proxy/strategy/base.py`<br>
`proxy/strategy/factory.py`<br>
`proxy/strategy/round_robin.py`<br>

维护者：heyao

---

### 260201 Proxy_CLI显示输出功能

(1)支持proxy_CLI开发，显示输出实例池、proxy信息，并维护使用方法<br>

涉及修改文件:<br>
`proxy/proxy_cli.py`<br>
`proxy/README.md`<br>

维护者：heyao

---

### 260131 一些有关Instance的功能完善

(1)Instance支持启动多个不同端口号的实例，解决了多个Instance下proxy注册失败问题<br>
(2)优化proxy和Instance之间的交互日志输出<br>

涉及修改文件:<br>
`proxy/resource/p_control_plane.py`<br>
`instance/instance_api.py`<br>
`test/demo_instance.py`<br>

维护者：heyao

---

### 260130 (v0.1.1) Proxy与Instance之间的接口功能完善

(1)实现proxy控制平面逻辑Fastapi(8002)，供Instance调用register/heartbeat/unregister/list<br>
(2)构建描述Instance状态的结构体`InstancePool`，描述Instance池ID，Instance负载信息等状态<br>
(3)支持proxy在lifespan启动时构建InstancePool，注入控制平面并启动控制平面。支持proxy在注销时在注销时退出控制平面并上报scheduler。<br>
(4)支持Instance与proxy控制平面之间的交互，register/heartbeat/unregister<br>

涉及修改文件:<br>
`proxy/proxy.py`<br>
`core/config.py`<br>
`instance/instance_api.py`<br>
`test/demo_instance.py`<br>

涉及新增文件:<br>
`proxy/resource/instance_pool.py`<br>
`proxy/resource/p_control_plane.py`<br>
`instance/pclient/proxy_client.py`<br>

维护者：heyao

---

### 260129 一些有关Proxy的功能完善

(1)构建与scheduler对接的proxy交互方法，使得proxy在启动时自动注册，然后动态发心跳包保活，退出后自动注销<br>
(2)新增`proxy/sclient`目录，用于维护与scheduler交互的client方法<br>
(3)新增`proxy/metrics`目录，用于后续处理从instance池抓取资源的整合处理，随后通过心跳包上传至scheduler<br>
(4)proxy依然是一个双平面结构（业务平面+控制平面）业务平面默认端口8001，proxy在初始化时应向scheduler注册的端口。控制平面默认8002，它用于作为instance交互proxy的端口，用于更新instance状态并维护instance池状态。proxy业务平面执行策略时将instance池状态作为输入执行具体策略。<br>

涉及修改文件:<br>
`proxy/proxy.py`<br>
`core/config.py`<br>

涉及新增文件:<br>
`proxy/sclient/scheduler_client.py`<br>
`proxy/metrics/local_metrics.py`

维护者：heyao

---

### 260128 scheduler控制平面维护结构构建，proxy对接接口构建

(1)新增维护proxy信息的结构体，包含静态信息（如注册时携带的信息`proxy_id/host/port/endpoints/tags/weight/meta`），和动态信息（如`load`，`last_seen`）。scheduler在初始化时会启用业务和控制两个平面，[业务平面]-[池信息结构体]-[控制平面]。scheduler在startup时创建pool。控制平面`control_plane.py`动态修改Proxy资源池信息，后续业务平面执行调度策略时会抓取proxy资源池信息。<br>
(2)scheduler实现基于轮询的调度策略，根据可用proxy选择轮询。scheduler内新建strategy目录，用于定义后续scheduler各种策略的具体实现方法。<br>
(3)demo_scheduler，新增`-- strategy`可选项，用于启动scheduler时选择调度策略<br>
(4)调度策略判定从scheduler主循环迁移至build_request，优化结构<br>
(5)丰富scheduler的cli功能，能够查看proxy池简易状态<br>

涉及修改文件:<br>
`core/config.py`<br>
`core/request.py`<br>
`scheduler/resource/control_plane.py`<br>
`scheduler/scheduler.py`<br>
`scheduler/scheduler_cli.py`<br>
`test/demo_scheduler.py`<br>

涉及新增文件:<br>
`scheduler/resource/proxy_pool.py`<br>
`scheduler/strategy/base.py`<br>
`scheduler/strategy/factory.py`<br>
`scheduler/strategy/round_robin.py`<br>

维护者：heyao

---

### 260127 说明性文件更新，scheduler控制平面接口部署，一些接口整理

(1)更新env/README.md: 构建新版本的vllm+LMcache镜像的操作步骤<br>
(2)新增启动脚本，支持容器容器和多开窗口，提升测试效率<br>
(3)抽离demo_scheduler.py本地配置参数，统一送入core/config文件<br>
(4)调整了scheduler内目录结构，按功能新增knowledge（用于维护知识清单）和resource（维护可用proxy及其计算网络资源）<br>
(5)新增scheduler的控制平面接口，它在scheduler初始化时被自动拉起监听，用于与proxy交互进行proxy注册以及后续的资源同步。

涉及修改文件:<br>
`env/README.md`<br>
`core/config.py`<br>
`test/demo_scheduler.py`<br>
`scheduler/scheduler.py`<br>

涉及新增文件:<br>
`test/quick_start_docker.sh`<br>
`scheduler/resource/control_plane.py`<br>


维护者：chen, heyao

---

### 260126 大更新(v0.1.0)：重构scheduler，proxy和request部分的知识库维护部分，不再依赖本地yaml预设值。而是实现scheduler启动时抓取KDN服务器中的知识索引，构建自己的知识清单。

(1)KDN_server的/search/text支持按field回传，而不是每次都回传整个结构体<br>
(2)KDN_server支持/snapshot整个知识库状态用于scheduler更新<br>
(3)更新scheduler，摒弃之前的本地yaml构建方式，支持启动初始化从kdn进行snapshot抓取并构建知识清单。<br>
(4)更新knowledge_base，支持sha256至int64映射（注：Faiss是通过INT64检索，而KDN形成的是sha256的str表达格式，所以在snap后需要进行映射。）<br>
(5)优化scheduler_CLI，增强信息维护和交互命令行接口<br>
(6)scheduler新增功能，动态同步KDN知识库状态，采用两阶段增量刷新，第一阶段拉轻量元信息，对于变更项才拉取二阶段<br>

涉及修改文件:<br>
`kdn_server/text_db.py`<br>
`kdn_server/kdn_api.py`<br>
`scheduler/__init__.py`<br>
`scheduler/scheduler.py`<br>
`scheduler/kdn_client.py`<br>
`scheduler/scheduler_cli`<br>
`store/knowledge_base.py`<br>
`core/request.py`<br>
`proxy/proxy.py`<br>
`test/demo_scheduler.py`<br>

涉及新增文件:<br>
`scheduler/kdn_sync.py`<br>

维护者：heyao

---

### 260123：完善了一些KDN服务器功能

(1)集成`kv_builder`状态位，扩展文本块的数据位，能够通过kid查到有无KVCache。
在 SQLite 里加 KV 元字段，并让 kv_builder 在完成 build 后回写。<br>
(2)将所有接口都集成到了`kdn_register_cli.py`，可以通过它统一注册、查询文本知识和KVCache块。<br>

涉及修改文件:<br>
`kdn_server/text_db.py`<br>
`kdn_server/kdn_api.py`<br>
`kdn_server/kv_builder.py`<br>
`kdn_server/kdn_register_cli.py`<br>
`scheduler/kdn_client.py`<br>

维护者：heyao

---

### 260121：构建了KDN服务器数据结构

(1)规范化KDN的知识块存储与命名,更新KDN结构，将文本块hash生成唯一id，同时采用sqlite3进行索引构建。<br>
(2)构造KVCache库,支持从Redis将CacheGen压缩的KVCache数据存储到本地，便于后续再利用。<br>
(3)实现对KV_database中KVCache向Redis服务器的重新注入。<br>
(4)维护kdn_server的`README.md`

涉及修改文件:<br>
`test/demo_kdn.py`<br>
`kdn_server/kdn_api.py`<br>

涉及新增文件:<br>
`kdn_server/text_db.py`<br>
`kdn_server/kdn_register_cli.py`<br>
`kdn_server/kv_builder.py`<br>
`kdn_server/kv_injector.py`<br>
`util/kdn_build_kv.py`<br>

维护者：heyao

---

### 260120：一些系统优化

(1)优化client.py的流式传输显示。<br>
(2)解决proxy在chat/completion模式下，无法嵌入知识的问题。<br>

涉及修改文件:<br>
`client/client.py`<br>
`proxy/proxy.py`<br>

维护者：heyao

---



