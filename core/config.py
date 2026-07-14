# config.py
"""
Configuration module: defines default parameters and constants
- Contains constant definitions only; no runtime logic
- Centralizes all default values
- Provides a clear configuration reference for deployment
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

# Default model settings
DEFAULT_MODEL = "/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct"  # Default main model path, used when Scheduler/Client does not specify one
DEFAULT_MODEL_SHORTNAME = "llama3-70b"  # Default short model name for request model fields and startup script examples
DEFAULT_EMBED_MODEL = "intfloat/multilingual-e5-large-instruct"  # Default embedding model name, used as a fallback when no local path is configured

# ====================================================================#
#                               Client                                #
# ====================================================================#
# Required and optional client fields; adjust validation here without touching other code.
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
SCHEDULER_LOG_FILE = "/workspace/llm-stack/CacheRoute/log/scheduler/"      # Log output path
SCHEDULER_VERBOSE_REQUEST_LOG=1                                            # Enable detailed request scheduling logs when set to 1

SCHEDULER_BASE_URL = "http://127.0.0.1:7001"                   # Scheduler data-plane URL, the default client entry point
SCHEDULER_CP_URL = "http://127.0.0.1:7002"                     # Scheduler control-plane URL for KDN/Proxy registration and heartbeats
EMBEDDING_MODEL = "/workspace/llm-stack/CacheRoute/model/embedder/intfloat__multilingual-e5-large-instruct"  # Embedding model path used by scheduler retrieval
KNOWLEDGE_YAML_PATH = ROOT_DIR / "data" / "knowledge_base.yaml"  # Local knowledge manifest YAML, usable in non-KDN mode

# Timeout settings
AIOHTTP_TIMEOUT = 6 * 60 * 60  # 6 hours
SCHEDULER_KDN_AUTO_REFRESH_DEFAULT = True                       # Scheduler automatically triggers KDN refreshes
SCHEDULER_KDN_REFRESH_INTERVAL_S_DEFAULT = 30                   # Interval in seconds for scheduler-triggered KDN knowledge refreshes

# Knowledge matching parameters
SCHEDULER_RETRIEVAL_TOP_K = 1                                   # Top-k candidates retained for each knowledge retrieval
SCHEDULER_RETRIEVAL_MIN_SCORE = 0.25                            # Minimum embedding score threshold; filters empty/weak matches, with 0.2-0.35 common for cosine scores
SCHEDULER_RETRIEVAL_MIN_RATIO = 0.75                            # Embedding similarity ratio threshold; keep only scores >= best*0.75 to reduce knowledge pollution

# Control plane
CONTROL_PLANE_TTL_S = 30                                        # Control-plane TTL in seconds
HEARTBEAT_INTERVAL_S = 5                                        # Heartbeat interval in seconds
SCHEDULER_HB_REPORT_INTERVAL_S = 30                             # Heartbeat log report interval in seconds

SCHEDULER_DP_PORT = 7001                                        # Data-plane listen port
SCHEDULER_DP_HOST = "127.0.0.1"                                 # Data-plane listen host
SCHEDULER_CP_PORT = 7002                                        # Control-plane listen port
SCHEDULER_CP_HOST = "127.0.0.1"                                 # Control-plane listen host

# Default scheduler strategy configuration (CacheRoute)
SCHEDULER_DEFAULT_STRATEGY = "round_robin"                      # Default scheduler strategy when demo scripts omit --strategy
SCHEDULER_CACHEROUTE_KDN_QPS_OVERLOAD_TH = 0.0                 # CacheRoute: KDN QPS threshold; values > 0 enable overload detection
SCHEDULER_CACHEROUTE_KDN_ITEMS_OVERLOAD_TH = 0                 # CacheRoute: KDN item-count threshold; values > 0 enable overload detection
SCHEDULER_CACHEROUTE_KDN_PENDING_OVERLOAD_TH = 0               # CacheRoute: KDN pending_transfers threshold; values > 0 enable this check
SCHEDULER_CACHEROUTE_KDN_ACTIVE_OVERLOAD_TH = 0                # CacheRoute: KDN active_transfers threshold; values > 0 enable this check
SCHEDULER_CACHEROUTE_KDN_QUEUE_MS_OVERLOAD_TH = 0.0            # CacheRoute: KDN queue EMA threshold in ms; values > 0 enable this check
SCHEDULER_CACHEROUTE_PROXY_LOAD_RATIO_DELTA = 0.1              # CacheRoute: proxy load-ratio safety window delta (0-1)
SCHEDULER_CACHEROUTE_PROXY_INFLIGHT_DELTA = 2                  # CacheRoute: proxy safety window (min_inflight + delta)
SCHEDULER_CACHEROUTE_PROXY_GPU_DELTA = 0.0                     # CacheRoute: proxy GPU safety window; 0 disables it
SCHEDULER_CACHEROUTE_AFFINITY_DECAY = 0.9                      # CacheRoute: knowledge-affinity history decay factor
SCHEDULER_CACHEROUTE_AFFINITY_TOPK = 256                       # CacheRoute: maximum affinity kid entries kept per proxy
SCHEDULER_CACHEROUTE_LOG_DECISION = 1                          # CacheRoute: emit one decision log line per request; 1 on, 0 off
# ====================================================================#
#                               Proxy                                 #
# ====================================================================#
PROXY_BASE_URL = "http://127.0.0.1:8001"                       # Default Proxy data-plane URL
PROXY_CP_URL = "http://127.0.0.1:8002"                         # Default Proxy control-plane URL
PROXY_DP_HOST = "127.0.0.1"                                    # Proxy data-plane listen host
PROXY_DP_PORT = 8001                                            # Proxy data-plane listen port
PROXY_CP_HOST = "127.0.0.1"                                    # Proxy control-plane listen host
PROXY_CP_PORT = 8002                                            # Proxy control-plane listen port

INSTANCE_ALIVE_TTL_S = 30                                      # Instance heartbeat TTL from the Proxy perspective, in seconds

PROXY_MAX_CAPACITY = 8                                          # Maximum concurrent tasks supported by the Proxy-managed instance pool; used to estimate queueing
PROXY_INSTANCE_COUNT = 1                                        # Number of instance devices managed by Proxy
PROXY_KV_MEM_PER_INSTANCE_GB = 128                              # KVCache capacity per Proxy-managed instance device
PROXY_KV_CACHE_UPDATE_POLICY = "lru"                            # KVCache update policy for Proxy-managed instances
PROXY_KDN_LINKS_JSON = ""                                       # Optional static topology tier JSON string

PREPARE_CONCURRENCY = 8                                         # Maximum concurrent knowledge preparation tasks per Proxy instance
READY_CONCURRENCY = 8                                           # Maximum concurrent inference tasks per Proxy instance
# ====================================================================#
#                              Instance                               #
# ====================================================================#
INSTANCE_BASE_URL = "http://127.0.0.1:9001"                    # Default Instance data-plane URL
INSTANCE_HOST = "127.0.0.1"                                    # Instance listen host
INSTANCE_PORT = 9001                                            # Instance listen port
INSTANCE_CP_HOST = "127.0.0.1"                                 # Instance control-plane listen host
INSTANCE_CP_PORT = 9002                                         # Instance control-plane listen port
VLLM_BASE_URL = "http://127.0.0.1:8000"                        # Downstream vLLM OpenAI-compatible API URL
USE_MOCK = False                                 # Local testing flag

INSTANCE_REDIS_HOST = "127.0.0.1"                              # Redis host used by Instance for KV injection/reuse
INSTANCE_REDIS_PORT = 6379                                     # Redis port
INSTANCE_REDIS_DB = 0                                          # Redis database number
INSTANCE_REDIS_PASSWORD = None                                 # Redis password; None means no password
INSTANCE_TOPOLOGY_KDN_TARGETS = ""                             # Instance auto topology discovery targets, comma-separated host:port or URLs
INSTANCE_DEFAULT_LINK_BW_MBPS = 1000.0                         # Fallback link bandwidth when NIC speed cannot be read

# Instance resource monitoring, enabled by default in demos; disable with --no-resource-monitor or INSTANCE_RESOURCE_MONITOR_ENABLE=0
INSTANCE_RESOURCE_MONITOR_ENABLE = True
INSTANCE_RESOURCE_AUTO_START_AGENT = True
INSTANCE_RESOURCE_AGENT_HOST = "127.0.0.1"
INSTANCE_RESOURCE_AGENT_PORT = 9201
INSTANCE_RESOURCE_AGENT_LISTEN = "127.0.0.1:9201"
INSTANCE_RESOURCE_AGENT_URL = "http://127.0.0.1:9201"
INSTANCE_RESOURCE_AGENT_SAMPLE_INTERVAL_MS = 1000
INSTANCE_RESOURCE_AGENT_START_TIMEOUT_S = 60.0
INSTANCE_RESOURCE_REPORT_ENABLE = False
INSTANCE_RESOURCE_REPORT_HZ = 1.0
INSTANCE_RESOURCE_REPORT_INTERVAL_MS = 1000
INSTANCE_RESOURCE_REPORT_TIMEOUT_S = 2.0

# ====================================================================#
#                               Other                                 #
# ====================================================================#
CLIENT_URL = "http://127.0.0.1:7071"                           # Local demo client service URL
# Default proxy configuration
DEFAULT_PREFILL = ["172.18.0.169:8001"]                        # Reserved: default prefill proxy list
DEFAULT_DECODE = ["172.18.0.169:8082"]                         # Reserved: default decode proxy list


# ====================================================================#
#                             KDN Server                              #
# ====================================================================#
KDN_BASE_URL = "http://127.0.0.1:9101"                          # KDN server URL
KDN_HOST = "127.0.0.1"                                         # KDN listen host
KDN_PORT = 9101                                                 # KDN listen port
KDN_NETWORK_ENABLE = False                                      # Whether KDN network simulation is enabled by default
KDN_NETWORK_BW_MB_S = 125.0                                     # Total bandwidth for KDN network simulation (MB/s)
KDN_NETWORK_BATCH_WINDOW_MS = 10.0                              # Batch window for KDN network simulation (ms)
KDN_NETWORK_FIXED_LATENCY_MS = 10.0                             # Fixed latency for KDN network simulation (ms)
KDN_NETWORK_EFFICIENCY = 0.8                                    # Bandwidth efficiency factor for KDN network simulation (0,1]
KDN_REDIS_REWRITE_ENABLE = False                                # Whether KDN rewrites Redis target addresses; disabled by default and does not affect the original path
KDN_REWRITE_LOOPBACK_TO = ""                                    # Optional target for rewriting loopback addresses only
KDN_FORCE_REDIS_HOST = ""                                       # Optional override for all Redis target addresses
DEFAULT_WARN_LEN = 4000                                         # Warn when registered text exceeds this length; file-path registration is recommended
# build_kv defaults, kept consistent with server defaults
DEFAULT_API_URL = "http://127.0.0.1:8000/v1/chat/completions"   # vLLM service URL used when building KVCache blocks
DEFAULT_MAX_TOKENS = 1                                          # Maximum decode tokens, usually 1
DEFAULT_TEMPERATURE = 0.0                                       # Temperature model parameter; does not affect KVCache blocks
DEFAULT_REDIS_HOST = "127.0.0.1"                                # Redis storage host
DEFAULT_REDIS_PORT = 6379                                       # Redis storage port
DEFAULT_REDIS_DB = 0                                            # Redis DBSIZE is expected to be 0 at initialization because differential capture is used
DEFAULT_MATCH = "vllm@*"                                        # KEYS prefix used when dumping KVCache
DEFAULT_SCAN_COUNT = 1000                                       # Scan count; the default is sufficient







# Service configuration
DEFAULT_PORT = 8081                                             # Reserved: legacy module default service port
DEFAULT_HOST = "172.18.0.169"                                   # Reserved: legacy module default service host

# Command dispatch timeout
DISPATCH_TIMEOUT = 10  # 10s

# Report threshold
REPORT_THRESHOLD = 1                                            # Reserved: status report threshold

# Report label
REPORT_LABEL = "172.18.0.169:8081"                              # Reserved: status report label

# Sync setting: whether to enable synchronization
USE_SYN = True                                                  # Reserved: whether to enable synchronized dispatch mode
# Sync setting: batch size
SYN_BATCH_SIZE = 3                                              # Reserved: per-batch size in sync mode
# Sync setting: wait timeout in seconds
SYN_TIMEOUT = 3                                                 # Reserved: sync-mode wait timeout in seconds

# RDMA configuration: protocol, choose "tcp" or "rdma"
MOONCAKE_PROTOCOL = "tcp"                                       # Reserved: Mooncake communication protocol (tcp/rdma)
# RDMA configuration: device name, choose "" or "mlx5_0"
MOONCAKE_DEVICE_NAME = ""                                       # Reserved: RDMA device name; empty means unspecified
