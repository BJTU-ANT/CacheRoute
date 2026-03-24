# demo_scheduler.py
"""
Scheduler_v1启动demo：
  - 引用 scheduler.py 里的 api
  - 使用 uvicorn 启动 HTTP 服务
"""
import argparse
import os, logging
import uvicorn

from scheduler import scheduler
from core import config

# 在 demo 里配置要预热的模型路径以及知识库路径
MODEL_PATH = config.DEFAULT_MODEL
KNOWLEDGE_YAML_PATH = config.KNOWLEDGE_YAML_PATH
EMBEDDING_MODEL = config.EMBEDDING_MODEL
KDN_BASE_URL = config.KDN_BASE_URL
dp_port = config.SCHEDULER_DP_PORT
dp_host = config.SCHEDULER_DP_HOST


def main():
    # logging配置
    logging.basicConfig(
        level=logging.INFO,  # 根 logger 级别设为 INFO
        format=" [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default="round_robin", help="proxy scheduling strategy")
    parser.add_argument(
        "--cacheroute",
        action="store_true",
        help="shortcut for --strategy cacheroute",
    )
    args = parser.parse_args()

    strategy_name = "cacheroute" if args.cacheroute else args.strategy

    # 把模型路径暴露给 scheduler（scheduler.py 里通过 os.getenv 读取）
    os.environ["SCHEDULER_MODEL_PATH"] = MODEL_PATH
    os.environ["SCHEDULER_TOKENIZER_MAP"]='{"llama3-70b":"/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct"}'
    os.environ["SCHEDULER_KNOWLEDGE_YAML"] = str(KNOWLEDGE_YAML_PATH)
    os.environ["SCHEDULER_KDN_BASE_URL"] = str(KDN_BASE_URL).rstrip("/")
    os.environ["SCHEDULER_EMBEDDING_MODEL"] = EMBEDDING_MODEL
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["SCHEDULER_STRATEGY"] = strategy_name

    # 配置 uvicorn.Server
    config = uvicorn.Config(scheduler, host=dp_host, port=dp_port, reload=False)
    server = uvicorn.Server(config)
    server.run()
    print("[DEMO] Scheduler stopped, demo exit.")


if __name__ == "__main__":
    main()
