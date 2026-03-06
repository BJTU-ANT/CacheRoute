# kdn_server/kv_inject.py
from __future__ import annotations

import argparse
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import redis


def b64url_decode(s: str) -> bytes:
    # urlsafe base64 without '=' padding -> restore padding then decode
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


@dataclass
class InjectResult:
    injected: int
    missing_files: int
    keys_b64url: List[str]


class KVCacheInjector:
    """
    从 KV_database/<kid>/manifest.jsonl 读取 key -> dump_file 映射，
    将 dump 的 value bytes 原样注入到目标 Redis。
    """

    def __init__(
        self,
        redis_host: str = "127.0.0.1",
        redis_port: int = 6379,
        redis_db: int = 0,
        redis_password: Optional[str] = None,
        socket_timeout_s: int = 30,
    ):
        self.rds = redis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            password=redis_password,
            decode_responses=False,  # 必须 False：保证 key/value 都是 bytes
            socket_timeout=socket_timeout_s,
        )

    def inject_kv_dir(self, kv_dir: str, return_keys: bool = True) -> InjectResult:
        kv_path = Path(kv_dir).resolve()
        manifest_path = kv_path / "manifest.jsonl"
        if not manifest_path.exists():
            raise FileNotFoundError(f"manifest.jsonl not found: {manifest_path}")

        injected = 0
        missing_files = 0
        keys_b64url: List[str] = []

        with manifest_path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue

                try:
                    rec = json.loads(line)
                except Exception as e:
                    raise ValueError(f"invalid json at {manifest_path}:{line_no}: {e}") from e

                key_b64 = rec.get("key_b64url")
                rel_file = rec.get("file")  # e.g. blocks/<name>.dump
                if not isinstance(key_b64, str) or not isinstance(rel_file, str):
                    raise ValueError(f"manifest record missing fields at line {line_no}: {rec}")

                key = b64url_decode(key_b64)
                dump_path = kv_path / rel_file
                if not dump_path.exists():
                    missing_files += 1
                    continue

                value = dump_path.read_bytes()

                # 覆盖/注入：不清空现有 redis，只写入这些 key
                self.rds.set(key, value)
                injected += 1

                if return_keys:
                    keys_b64url.append(key_b64)

        return InjectResult(injected=injected, missing_files=missing_files, keys_b64url=keys_b64url)


def main():
    ap = argparse.ArgumentParser(description="Inject KVCache dumps into target Redis (no flushdb).")
    ap.add_argument("--kv-dir", required=True, help="KV_database/<kid> directory containing manifest.jsonl")
    ap.add_argument("--redis-host", default="127.0.0.1")
    ap.add_argument("--redis-port", type=int, default=6379)
    ap.add_argument("--redis-db", type=int, default=0)
    ap.add_argument("--redis-password", default=None)
    ap.add_argument("--no-return-keys", action="store_true", help="Do not print key list (faster, less output)")
    args = ap.parse_args()

    injector = KVCacheInjector(
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        redis_db=args.redis_db,
        redis_password=args.redis_password,
    )
    res = injector.inject_kv_dir(args.kv_dir, return_keys=not args.no_return_keys)

    # 打印结果：测试阶段你就能直接看到 keys 是否符合预期
    out: Dict = {
        "kv_dir": str(Path(args.kv_dir).resolve()),
        "injected": res.injected,
        "missing_files": res.missing_files,
    }
    if not args.no_return_keys:
        out["keys_b64url"] = res.keys_b64url

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

