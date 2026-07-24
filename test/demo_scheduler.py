# demo_scheduler.py
"""
Scheduler_v1 startup demo:
  - Import the API from scheduler.py
  - Start the HTTP service with uvicorn
"""
import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

# Allow direct execution from test/ while importing project packages from the
# repository root, consistent with the other demo entrypoints.
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scheduler import scheduler
from core import config

# Configure the model path to warm up and the knowledge-base path in the demo
MODEL_PATH = config.DEFAULT_MODEL
KNOWLEDGE_YAML_PATH = config.KNOWLEDGE_YAML_PATH
EMBEDDING_MODEL = config.EMBEDDING_MODEL
KDN_BASE_URL = config.KDN_BASE_URL
dp_port = config.SCHEDULER_DP_PORT
dp_host = config.SCHEDULER_DP_HOST
SCHEDULER_DEFAULT_STRATEGY = config.SCHEDULER_DEFAULT_STRATEGY
SCHEDULER_CACHEROUTE_KDN_PENDING_OVERLOAD_TH = config.SCHEDULER_CACHEROUTE_KDN_PENDING_OVERLOAD_TH
SCHEDULER_CACHEROUTE_KDN_ACTIVE_OVERLOAD_TH = config.SCHEDULER_CACHEROUTE_KDN_ACTIVE_OVERLOAD_TH
SCHEDULER_CACHEROUTE_KDN_QUEUE_MS_OVERLOAD_TH = config.SCHEDULER_CACHEROUTE_KDN_QUEUE_MS_OVERLOAD_TH
SCHEDULER_CACHEROUTE_LOG_DECISION = config.SCHEDULER_CACHEROUTE_LOG_DECISION


def main():
    # logging configuration
    logging.basicConfig(
        level=logging.INFO,  # set the root logger level to INFO
        format=" [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default=SCHEDULER_DEFAULT_STRATEGY, help="proxy scheduling strategy")
    parser.add_argument(
        "--cacheroute",
        action="store_true",
        help="shortcut for --strategy cacheroute",
    )
    parser.add_argument("--kdn-pending-overload-th", type=int, default=SCHEDULER_CACHEROUTE_KDN_PENDING_OVERLOAD_TH, help="CacheRoute KDN pending overload threshold")
    parser.add_argument("--kdn-active-overload-th", type=int, default=SCHEDULER_CACHEROUTE_KDN_ACTIVE_OVERLOAD_TH, help="CacheRoute KDN active overload threshold")
    parser.add_argument("--kdn-queue-ms-overload-th", type=float, default=SCHEDULER_CACHEROUTE_KDN_QUEUE_MS_OVERLOAD_TH, help="CacheRoute KDN queue-ms overload threshold")
    parser.add_argument("--proxy-load-ratio-delta", type=float, default=config.SCHEDULER_CACHEROUTE_PROXY_LOAD_RATIO_DELTA, help="CacheRoute proxy load-ratio safety delta in [0,1]")
    parser.add_argument(
        "--cacheroute-log-decision",
        type=int,
        choices=[0, 1],
        default=SCHEDULER_CACHEROUTE_LOG_DECISION,
        help="CacheRoute one-line decision log switch: 1=on, 0=off",
    )
    args = parser.parse_args()

    strategy_name = "cacheroute" if args.cacheroute else args.strategy

    # Expose the model path to scheduler; scheduler.py reads it through os.getenv
    os.environ["SCHEDULER_MODEL_PATH"] = MODEL_PATH
    os.environ["SCHEDULER_TOKENIZER_MAP"]='{"llama3-70b":"/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct"}'
    os.environ["SCHEDULER_KNOWLEDGE_YAML"] = str(KNOWLEDGE_YAML_PATH)
    os.environ["SCHEDULER_KDN_BASE_URL"] = str(KDN_BASE_URL).rstrip("/")
    os.environ["SCHEDULER_EMBEDDING_MODEL"] = EMBEDDING_MODEL
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["SCHEDULER_STRATEGY"] = strategy_name
    os.environ["SCHEDULER_CACHEROUTE_KDN_PENDING_OVERLOAD_TH"] = str(args.kdn_pending_overload_th)
    os.environ["SCHEDULER_CACHEROUTE_KDN_ACTIVE_OVERLOAD_TH"] = str(args.kdn_active_overload_th)
    os.environ["SCHEDULER_CACHEROUTE_KDN_QUEUE_MS_OVERLOAD_TH"] = str(args.kdn_queue_ms_overload_th)
    os.environ["SCHEDULER_CACHEROUTE_PROXY_LOAD_RATIO_DELTA"] = str(args.proxy_load_ratio_delta)
    os.environ["SCHEDULER_CACHEROUTE_LOG_DECISION"] = str(args.cacheroute_log_decision)

    # Configure uvicorn.Server
    server_config = uvicorn.Config(scheduler, host=dp_host, port=dp_port, reload=False)
    server = uvicorn.Server(server_config)
    server.run()
    print("[DEMO] Scheduler stopped, demo exit.")


if __name__ == "__main__":
    main()
