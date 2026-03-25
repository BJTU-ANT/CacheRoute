import os, sys
import uvicorn
import argparse,logging
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from kdn_server.kdn_api import kdn
from core import config

KDN_TEXT_DB_DIR = ROOT_DIR / "kdn_server" / "text_database"
KDN_DATA_YAML = ROOT_DIR / "data" / "kdn_text_base.yaml"
KDN_KV_DB_DIR = ROOT_DIR / "kdn_server" / "KV_database"

def build_args():
    parser = argparse.ArgumentParser(description="Run KDN server")

    parser.add_argument("--host", type=str, default=config.KDN_HOST)
    parser.add_argument("--port", type=int, default=config.KDN_PORT)

    # network simulation
    parser.add_argument(
        "--network",
        action="store_true",
        help="Enable simulated network delay for KV injection ack",
    )
    parser.add_argument(
        "--network-bw-mb-s",
        type=float,
        default=config.KDN_NETWORK_BW_MB_S,
        help="Total simulated network bandwidth in MB/s",
    )
    parser.add_argument(
        "--network-batch-window-ms",
        type=float,
        default=config.KDN_NETWORK_BATCH_WINDOW_MS,
        help="Batching window for simulated network scheduler in ms",
    )
    parser.add_argument(
        "--network-fixed-latency-ms",
        type=float,
        default=config.KDN_NETWORK_FIXED_LATENCY_MS,
        help="Fixed per-transfer latency in ms",
    )
    parser.add_argument(
        "--network-efficiency",
        type=float,
        default=config.KDN_NETWORK_EFFICIENCY,
        help="Bandwidth efficiency factor in (0, 1]",
    )

    return parser.parse_args()

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        force=True,
    )

    args = build_args()

    # 这里暴露配置（你想要的“demo里配置，不在模块里写死”）
    os.environ["KDN_TEXT_DB_DIR"] = str(KDN_TEXT_DB_DIR)
    os.environ["KDN_KV_DB_DIR"] = str(KDN_KV_DB_DIR)
    os.environ[
        "KDN_EMBEDDING_MODEL"] = "/workspace/llm-stack/CacheRoute/model/embedder/intfloat__multilingual-e5-large-instruct"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    os.environ.setdefault("SCHEDULER_CP_URL", config.SCHEDULER_CP_URL)
    os.environ.setdefault("KDN_ID", "kdn_local_1")
    os.environ.setdefault("KDN_ADVERTISE_HOST", args.host)
    os.environ.setdefault("KDN_ADVERTISE_PORT", str(args.port))

    # network simulator config
    os.environ["KDN_NETWORK_ENABLE"] = "1" if args.network else "0"
    os.environ["KDN_NETWORK_BW_MB_S"] = str(args.network_bw_mb_s)
    os.environ["KDN_NETWORK_BATCH_WINDOW_MS"] = str(args.network_batch_window_ms)
    os.environ["KDN_NETWORK_FIXED_LATENCY_MS"] = str(args.network_fixed_latency_ms)
    os.environ["KDN_NETWORK_EFFICIENCY"] = str(args.network_efficiency)

    print(
        "[demo_kdn] network config:",
        {
            "enabled": args.network,
            "bw_mb_s": args.network_bw_mb_s,
            "batch_window_ms": args.network_batch_window_ms,
            "fixed_latency_ms": args.network_fixed_latency_ms,
            "efficiency": args.network_efficiency,
        },
    )

uvicorn.run(kdn, host=config.KDN_HOST, port=config.KDN_PORT, log_level="info")
