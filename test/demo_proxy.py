"""
demo_proxy.py

启动 Proxy 服务，用于接收 Scheduler 转发的 Request payload。
"""

import uvicorn
import sys
import argparse
import os


from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from core import config
from proxy import proxy  # 确保与 Proxy.py 在同一包/目录下


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run CacheRoute Proxy")
    parser.add_argument("--host", type=str, default=None, help="proxy listen host (default from config/env)")
    parser.add_argument("--port", type=int, default=None, help="proxy listen port (default from config/env)")
    parser.add_argument(
        "--strategy",
        type=str,
        default=None,
        help="instance scheduling strategy (e.g., round_robin, least_inflight)",
    )
    args = parser.parse_args()

    if args.strategy:
        os.environ["PROXY_INSTANCE_STRATEGY"] = args.strategy

    # 选择一个与 Scheduler 不同的端口，例如 8001
    uvicorn.run(proxy, host=config.PROXY_DP_HOST, port=config.PROXY_DP_PORT, reload=False)
