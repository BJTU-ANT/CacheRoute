### 260201 Proxy_CLI显示输出功能

(1)支持proxy_CLI开发，显示输出实例池、proxy信息，并维护使用方法<br>

涉及修改文件:<br>
`proxy/proxy_cli.py`<br>
`proxy/README.md`<br>

一些提上日程的工作：<br>
(1)KDN服务器的UI搭建，重点是知识可读性（_TODO. chen_）<br>
(2)instance侧需要搭建一个灵活的资源检索平台(主要是基于vllm平台抓取信息)，使得instance面向proxy暴露动态更新的实例负载信息，便于proxy抓取（_TODO. sihan_）<br>
(3)scheduler对池级业务流状态维护(_TODO. heyao_)<br>
(4)proxy调度策略接入Instance池<br>

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



