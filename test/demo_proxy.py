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
    parser.add_argument(
        "--kdn-links-json",
        type=str,
        default=config.PROXY_KDN_LINKS_JSON,
        help="optional JSON string for PROXY_KDN_LINKS_JSON (CacheRoute topology tiers)",
    )
    parser.add_argument(
        "--injection-strategy",
        type=str,
        default=None,
        help="proxy injection strategy (default|iws)",
    )
    args = parser.parse_args()

    if args.strategy:
        os.environ["PROXY_INSTANCE_STRATEGY"] = args.strategy
    if args.injection_strategy:
        normalized = args.injection_strategy.strip().lower()
        if normalized not in {"default", "iws"}:
            parser.error("--injection-strategy must be one of: default, iws")
        os.environ["PROXY_INJECTION_STRATEGY"] = normalized
    if args.kdn_links_json and str(args.kdn_links_json).strip():
        os.environ["PROXY_KDN_LINKS_JSON"] = args.kdn_links_json

    from proxy import proxy  # 确保在设置环境变量后导入

    # 选择一个与 Scheduler 不同的端口，例如 8001
    uvicorn.run(proxy, host=config.PROXY_DP_HOST, port=config.PROXY_DP_PORT, reload=False)
