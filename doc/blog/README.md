### 260127 更新了env/README.md :完善了构建新版本的vllm+LMcache镜像的操作步骤
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



