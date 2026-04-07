import uvicorn
import argparse
import os
import sys

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from core import config

def main():
    # ====== 启动 Instance 服务 ======
    parser = argparse.ArgumentParser(description="Run CacheRoute Instance")
    parser.add_argument("--host", type=str, default=None, help="listen host (override config)")
    parser.add_argument("--port", type=int, default=None, help="listen port (override config)")
    parser.add_argument(
        "--kdn-targets",
        type=str,
        default=None,
        help="optional topology discovery targets, comma separated (e.g. 127.0.0.1:9101,127.0.0.1:9102)",
    )
    args = parser.parse_args()

    # 默认来自 config / env；若命令行提供则覆盖
    cfg_port = int(os.environ.get("INSTANCE_PORT", config.INSTANCE_PORT))
    cfg_host = os.environ.get("INSTANCE_HOST", config.INSTANCE_HOST)

    host = args.host if args.host is not None else cfg_host
    port = args.port if args.port is not None else cfg_port

    # 保证 “监听端口 == 注册上报端口”
    # instance_api.py 的 lifespan 读取 INSTANCE_ADVERTISE_HOST/PORT 与 INSTANCE_PORT
    os.environ["INSTANCE_ADVERTISE_HOST"] = host
    os.environ["INSTANCE_ADVERTISE_PORT"] = str(port)
    os.environ["INSTANCE_PORT"] = str(port)
    if args.kdn_targets:
        os.environ["INSTANCE_TOPOLOGY_KDN_TARGETS"] = args.kdn_targets.strip()

    # （可选但强烈建议）确保每个实例 id 唯一，避免 pool upsert 覆盖
    os.environ.setdefault("INSTANCE_ID", f"hp_{host}:{port}")

    from instance import instance
    uvicorn.run(instance, host=host, port=port, reload=False)

if __name__ == "__main__":
    main()
