# config.py
"""
配置模块：定义所有默认参数和常量
- 仅包含常量定义，不包含任何逻辑
- 所有默认值集中管理
- 为生产环境提供清晰的配置参考
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

# 默认模型型号
DEFAULT_MODEL = "/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct"  # 默认主模型路径（Scheduler/Client 未显式指定时使用）
DEFAULT_MODEL_SHORTNAME = "llama3-70b"  # 默认模型短名（用于请求体 model 字段与启动脚本示例）
DEFAULT_EMBED_MODEL = "intfloat/multilingual-e5-large-instruct"  # 默认embedding模型名称（当本地路径未配置时可回退）

# ====================================================================#
#                               Client                                #
# ====================================================================#
# 规范client的必备字段以及限制字段合集（后续要放宽/收紧，只改这里即可，不动其他代码。）
REQUIRED_FIELDS = {
    "chat": {"model", "messages"},
    "completions": {"model", "prompt"},
}
ALLOWED_OPTION_FIELDS = {
    "chat": {"temperature", "top_p", "max_tokens", "stream", "RAG", "Injection_type"},
    "completions": {"temperature", "top_p", "max_tokens", "RAG", "Injection_type"},
}


# ====================================================================#
#                             Scheduler                               #
# ====================================================================#
SCHEDULER_LOG_FILE = "/workspace/llm-stack/CacheRoute/log/scheduler/"      #日志输出路径
SCHEDULER_VERBOSE_REQUEST_LOG=1                                            #是否开启请求调度细节输出，1为开启

SCHEDULER_BASE_URL = "http://127.0.0.1:7001"                   # Scheduler业务平面URL（客户端默认请求入口）
SCHEDULER_CP_URL = "http://127.0.0.1:7002"                     # Scheduler控制平面URL（KDN/Proxy注册与心跳入口）
EMBEDDING_MODEL = "/workspace/llm-stack/CacheRoute/model/embedder/intfloat__multilingual-e5-large-instruct"  # Scheduler检索使用的embedding模型路径
KNOWLEDGE_YAML_PATH = ROOT_DIR / "data" / "knowledge_base.yaml"  # 本地知识清单yaml（非KDN模式可用）

# 超时配置
AIOHTTP_TIMEOUT = 6 * 60 * 60  # 6小时
SCHEDULER_KDN_AUTO_REFRESH_DEFAULT = True                       # scheduler会自动触发KDN更新
SCHEDULER_KDN_REFRESH_INTERVAL_S_DEFAULT = 30                   # scheduler自动触发KDN知识更新的频率（秒）

# 知识匹配参数
SCHEDULER_RETRIEVAL_TOP_K = 1                                   # 每次知识检索保留的top-k候选数
SCHEDULER_RETRIEVAL_MIN_SCORE = 0.25                            # embedding得分阈值下限，筛选空值，cosine 下常见起点：0.2~0.35
SCHEDULER_RETRIEVAL_MIN_RATIO = 0.75                            # embedding相似度门限，只保留 >= best*0.75，减轻检索知识污染

# 控制平面
CONTROL_PLANE_TTL_S = 30                                        # 控制平面TTL（s）
HEARTBEAT_INTERVAL_S = 5                                        # 心跳包间隔时间（s）
SCHEDULER_HB_REPORT_INTERVAL_S = 30                             # 心跳包日志输出时间（s）

SCHEDULER_DP_PORT = 7001                                        # 业务平面监听端口
SCHEDULER_DP_HOST = "127.0.0.1"                                 # 业务平面监听地址
SCHEDULER_CP_PORT = 7002                                        # 控制平面监听端口
SCHEDULER_CP_HOST = "127.0.0.1"                                 # 控制平面监听地址

# Scheduler策略默认配置（CacheRoute）
SCHEDULER_DEFAULT_STRATEGY = "round_robin"                      # Scheduler默认策略（demo未指定--strategy时生效）
SCHEDULER_CACHEROUTE_KDN_QPS_OVERLOAD_TH = 0.0                 # CacheRoute: KDN qps阈值，>0时启用过载判定
SCHEDULER_CACHEROUTE_KDN_ITEMS_OVERLOAD_TH = 0                 # CacheRoute: KDN items阈值，>0时启用过载判定
SCHEDULER_CACHEROUTE_KDN_PENDING_OVERLOAD_TH = 0               # CacheRoute: KDN pending_transfers阈值，>0时启用
SCHEDULER_CACHEROUTE_KDN_ACTIVE_OVERLOAD_TH = 0                # CacheRoute: KDN active_transfers阈值，>0时启用
SCHEDULER_CACHEROUTE_KDN_QUEUE_MS_OVERLOAD_TH = 0.0            # CacheRoute: KDN queue EMA(ms)阈值，>0时启用
SCHEDULER_CACHEROUTE_PROXY_LOAD_RATIO_DELTA = 0.1              # CacheRoute: proxy负载比例安全窗口delta（0~1）
SCHEDULER_CACHEROUTE_PROXY_INFLIGHT_DELTA = 2                  # CacheRoute: proxy安全窗口（min_inflight + delta）
SCHEDULER_CACHEROUTE_PROXY_GPU_DELTA = 0.0                     # CacheRoute: proxy GPU安全窗口（0表示不启用）
SCHEDULER_CACHEROUTE_AFFINITY_DECAY = 0.9                      # CacheRoute: 知识亲和历史衰减系数
SCHEDULER_CACHEROUTE_AFFINITY_TOPK = 256                       # CacheRoute: 每个proxy保留的亲和kid数量上限
SCHEDULER_CACHEROUTE_LOG_DECISION = 1                          # CacheRoute: 是否输出每请求一行决策日志（1开0关）
# ====================================================================#
#                               Proxy                                 #
# ====================================================================#
PROXY_BASE_URL = "http://127.0.0.1:8001"                       # Proxy业务平面默认URL
PROXY_CP_URL = "http://127.0.0.1:8002"                         # Proxy控制平面默认URL
PROXY_DP_HOST = "127.0.0.1"                                    # Proxy业务平面监听地址
PROXY_DP_PORT = 8001                                            # Proxy业务平面监听端口
PROXY_CP_HOST = "127.0.0.1"                                    # Proxy控制平面监听地址
PROXY_CP_PORT = 8002                                            # Proxy控制平面监听端口

INSTANCE_ALIVE_TTL_S = 30                                      # Proxy视角下Instance心跳TTL（秒）

PROXY_MAX_CAPACITY = 8                                          # Proxy管理实例池支持的最大并发任务数，衡量排队情况
PROXY_INSTANCE_COUNT = 1                                        # Proxy管理实例设备数量
PROXY_KV_MEM_PER_INSTANCE_GB = 128                              # Proxy管理实例设备的KVCache缓存大小
PROXY_KV_CACHE_UPDATE_POLICY = "lru"                            # Proxy管理实例的KVCache更新策略
PROXY_KDN_LINKS_JSON = ""                                       # 可选：静态拓扑 tier JSON 字符串

PREPARE_CONCURRENCY = 8                                         # Proxy每个实例允许的最大并发知识准备任务数
READY_CONCURRENCY = 8                                           # Proxy每个实例允许的最大并发推理任务数
# ====================================================================#
#                              Instance                               #
# ====================================================================#
INSTANCE_BASE_URL = "http://127.0.0.1:9001"                    # Instance业务平面默认URL
INSTANCE_HOST = "127.0.0.1"                                    # Instance监听地址
INSTANCE_PORT = 9001                                            # Instance监听端口
INSTANCE_CP_HOST = "127.0.0.1"                                 # Instance控制平面监听地址
INSTANCE_CP_PORT = 9002                                         # Instance控制平面监听端口
VLLM_BASE_URL = "http://127.0.0.1:8000"                        # 下游vLLM OpenAI兼容接口URL
USE_MOCK = False                                 # 本地测试标签

INSTANCE_REDIS_HOST = "127.0.0.1"                              # Instance侧访问Redis地址（KV注入/复用）
INSTANCE_REDIS_PORT = 6379                                     # Redis端口
INSTANCE_REDIS_DB = 0                                          # Redis数据库编号
INSTANCE_REDIS_PASSWORD = None                                 # Redis密码（None表示无密码）
INSTANCE_TOPOLOGY_KDN_TARGETS = ""                             # Instance自动拓扑发现目标（逗号分隔 host:port 或 URL）
INSTANCE_DEFAULT_LINK_BW_MBPS = 1000.0                         # 兜底链路带宽（无法读取网卡速率时）

# ====================================================================#
#                               Other                                 #
# ====================================================================#
CLIENT_URL = "http://127.0.0.1:7071"                           # demo client本地服务URL
# 默认代理配置
DEFAULT_PREFILL = ["172.18.0.169:8001"]                        # 预留：prefill默认代理列表
DEFAULT_DECODE = ["172.18.0.169:8082"]                         # 预留：decode默认代理列表


# ====================================================================#
#                             KDN Server                              #
# ====================================================================#
KDN_BASE_URL = "http://127.0.0.1:9101"                          # KDN服务器URL
KDN_HOST = "127.0.0.1"                                         # KDN监听地址
KDN_PORT = 9101                                                 # KDN监听端口
KDN_NETWORK_ENABLE = False                                      # KDN网络模拟默认是否启用
KDN_NETWORK_BW_MB_S = 125.0                                     # KDN网络模拟总带宽（MB/s）
KDN_NETWORK_BATCH_WINDOW_MS = 10.0                              # KDN网络模拟批处理窗口（ms）
KDN_NETWORK_FIXED_LATENCY_MS = 10.0                             # KDN网络模拟固定时延（ms）
KDN_NETWORK_EFFICIENCY = 0.8                                    # KDN网络模拟带宽效率系数（0,1]
DEFAULT_WARN_LEN = 4000                                         # 注册文本超过该长度则会警告，建议通过文件路径方式注册
# build_kv 的默认值（与你服务端默认保持一致）
DEFAULT_API_URL = "http://127.0.0.1:8000/v1/chat/completions"   # 构建KVCache块时送入的vllm服务url
DEFAULT_MAX_TOKENS = 1                                          # 最大decode token数，一般为1
DEFAULT_TEMPERATURE = 0.0                                       # 温度，模型参数，对KVCache块无影响
DEFAULT_REDIS_HOST = "127.0.0.1"                                # 存储的Redis服务器地址
DEFAULT_REDIS_PORT = 6379                                       # 存储的Redis服务器端口号
DEFAULT_REDIS_DB = 0                                            # 由于采用差分抓取，默认初始化时Redis服务器的DBSIZE=0
DEFAULT_MATCH = "vllm@*"                                        # 在dump KVCache时用的KEYS统一前缀
DEFAULT_SCAN_COUNT = 1000                                       # 扫描轮次，默认值即可







# 服务配置
DEFAULT_PORT = 8081                                             # 预留：历史模块默认服务端口
DEFAULT_HOST = "172.18.0.169"                                   # 预留：历史模块默认服务地址

# 下发指令超时时间
DISPATCH_TIMEOUT = 10  # 10s

# 上报阈值
REPORT_THRESHOLD = 1                                            # 预留：状态上报阈值

# 上报标签
REPORT_LABEL = "172.18.0.169:8081"                              # 预留：状态上报标签

# 同步设置 是否启用同步
USE_SYN = True                                                  # 预留：是否启用同步下发模式
# 同步设置 批处理批次
SYN_BATCH_SIZE = 3                                              # 预留：同步模式下每批处理大小
# 同步设置 等待时间 秒
SYN_TIMEOUT = 3                                                 # 预留：同步模式等待超时（秒）

# RDMA配置 协议  可选 "tcp" or "rdma"
MOONCAKE_PROTOCOL = "tcp"                                       # 预留：Mooncake通信协议（tcp/rdma）
# RDMA配置 设备名 可选 "" or "mlx5_0"
MOONCAKE_DEVICE_NAME = ""                                       # 预留：RDMA设备名（空表示不指定）
