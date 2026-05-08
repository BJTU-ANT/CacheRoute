### Core
涉及关键方法、结构体定义

- config.py:关键配置信息接口
- fwd.py: vLLM自带的http请求收发接口
- model_calculation.py:


CacheRoute支持大规模推理资源池互联与调度。为便于展示CacheRoute的功能，config内为便于展示demo，使用的是环回地址进行Scheduler、proxy、instance和kdn_server的配置，若设备允许，开展跨机实验，需要将地址进行拆分。这里以拆分KDN_server为例，proxy、instance的地址配置与kdn_server类似<br>
（1）需要确保连通性的KDN服务器与推理服务器，这里假设KDN服务器地址为`172.18.0.171`，推理服务器地址为`172.18.0.169`<br>
（2）在169设备的config中配置scheduler信息`SCHEDULER_BASE_URL = "http://172.18.0.169:7001"`，`SCHEDULER_CP_URL   = "http://172.18.0.169:7002"`，`SCHEDULER_DP_HOST = "0.0.0.0"`，`SCHEDULER_CP_HOST = "0.0.0.0"`<br>
（3）在169设备的config中配置proxy信息`PROXY_BASE_URL = "http://172.18.0.169:8001"`，`PROXY_CP_URL   = "http://172.18.0.169:8002"`，`PROXY_DP_HOST = "0.0.0.0"`，`PROXY_CP_HOST = "0.0.0.0"`<br>
（4）在169设备的config中配置instance信息`INSTANCE_BASE_URL = "http://172.18.0.169:9001"`,`INSTANCE_HOST = "0.0.0.0"`,`INSTANCE_CP_HOST = "0.0.0.0"`,`INSTANCE_REDIS_HOST = "127.0.0.1"`,`INSTANCE_TOPOLOGY_KDN_TARGETS = "http://172.18.0.171:9101"`<br>
（5）在171设备的config中配置KDN_server信息`KDN_BASE_URL = "http://172.18.10.171:9101"`,`KDN_HOST = "172.18.0.171"`,`SCHEDULER_CP_URL = "http://172.18.0.169:7002"`,`KDN_FORCE_REDIS_HOST = "172.18.0.169"`
