# kdn_server/kv_builder.py
from __future__ import annotations

import base64
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests
import redis

from .text_db import compute_kid, _normalize_text


def _b64url(s: bytes) -> str:
    # urlsafe base64 without trailing '='
    return base64.urlsafe_b64encode(s).decode("ascii").rstrip("=")


@dataclass
class KVBuildConfig:
    kv_root: str                    # host path: .../kdn_server/KV_database
    api_url: str                    # vLLM OpenAI-compatible endpoint
    model: str
    max_tokens: int = 1
    temperature: float = 0.0

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None

    match: str = "vllm@*"           # 你之前 dump 的 key pattern
    scan_count: int = 1000
    settle_wait_s: float = 0.2      # 请求后等 key 稳定的等待
    settle_rounds: int = 3          # 连续 N 轮 keys 数不变 => 认为稳定

    flushdb: bool = False           # 注意：会清空整个 redis db（危险）


class KVCacheBuilder:
    """
    统一的“触发推理 -> Redis 扫描 KV key -> dump 到 KV_database/<kid>/”实现。
    """

    def __init__(self, cfg: KVBuildConfig, text_db=None):
        self.cfg = cfg
        self.rds = redis.Redis(
            host=cfg.redis_host,
            port=cfg.redis_port,
            db=cfg.redis_db,
            password=cfg.redis_password,
            decode_responses=False,   # 必须 False：key/value 都按 bytes 处理
        )
        self.text_db = text_db


    def build_from_text_file(self, txt_path: str) -> Dict:
        p = Path(txt_path).resolve()
        content = p.read_text(encoding="utf-8")
        return self.build_from_text(content)


    def build_from_text(self, text: str) -> Dict:
        norm = _normalize_text(text)
        if not norm:
            raise ValueError("text is empty after normalization")

        kid = compute_kid(norm)
        out_dir = Path(self.cfg.kv_root).resolve() / kid

        # 覆盖刷新：删旧目录
        if out_dir.exists():
            shutil.rmtree(out_dir)
        (out_dir / "blocks").mkdir(parents=True, exist_ok=True)

        if self.cfg.flushdb:
            # 高危：会影响同 db 内其他任务
            self.rds.flushdb()

        # (A) 记录 before
        keys_before = self._scan_keys_set()

        # (B) 触发一次推理
        self._trigger_infer(norm)

        # (C) 等待稳定并取 after
        keys_after = self._wait_keys_settle_set()

        # (D) 差分：只 dump 新增 keys
        keys_new = list(keys_after - keys_before)

        # 3) dump keys -> files
        manifest_path = out_dir / "manifest.jsonl"
        dumped = self._dump_keys(keys_new, out_dir / "blocks", manifest_path)

        # 4) 写 run_meta
        meta = {
            "kid": kid,
            "time": int(time.time()),
            "api_url": self.cfg.api_url,
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
            "redis": {
                "host": self.cfg.redis_host,
                "port": self.cfg.redis_port,
                "db": self.cfg.redis_db,
                "match": self.cfg.match,
            },
            "dumped_keys": dumped,
            "keys_before": len(keys_before),
            "keys_after": len(keys_after),
            "keys_new": len(keys_new),
        }
        (out_dir / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        if self.text_db is not None:
            self.text_db.mark_kv_ready(kid=kid, kv_rel_dir=kid, dumped_keys=dumped, updated_at=meta["time"])

        return {"kid": kid, "kv_dir": str(out_dir), "dumped_keys": dumped}


    def _trigger_infer(self, prompt: str) -> None:
        # 你之前用的是 /v1/chat/completions，这里保持一致
        payload = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": prompt}],
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
            "stream": False,
        }
        r = requests.post(self.cfg.api_url, json=payload, timeout=300)
        r.raise_for_status()

    def _scan_keys_set(self) -> set[bytes]:
        return set(self._scan_keys())

    def _wait_keys_settle_set(self) -> set[bytes]:
        last_n = -1
        stable = 0
        keys_set: set[bytes] = set()

        for _ in range(max(1, self.cfg.settle_rounds * 3)):
            time.sleep(self.cfg.settle_wait_s)
            keys_set = self._scan_keys_set()
            n = len(keys_set)
            if n == last_n:
                stable += 1
                if stable >= self.cfg.settle_rounds:
                    break
            else:
                stable = 0
                last_n = n
        return keys_set

    def _scan_keys(self) -> List[bytes]:
        # SCAN match pattern
        cursor = 0
        out: List[bytes] = []
        while True:
            cursor, batch = self.rds.scan(cursor=cursor, match=self.cfg.match, count=self.cfg.scan_count)
            out.extend(batch)
            if cursor == 0:
                break
        return out


    def _wait_keys_settle(self) -> List[bytes]:
        last_n = -1
        stable = 0
        keys: List[bytes] = []

        for _ in range(max(1, self.cfg.settle_rounds * 3)):
            time.sleep(self.cfg.settle_wait_s)
            keys = self._scan_keys()
            n = len(keys)
            if n == last_n:
                stable += 1
                if stable >= self.cfg.settle_rounds:
                    break
            else:
                stable = 0
                last_n = n
        return keys


    def _dump_keys(self, keys: Iterable[bytes], blocks_dir: Path, manifest_path: Path) -> int:
        dumped = 0
        with manifest_path.open("w", encoding="utf-8") as mf:
            for k in keys:
                v = self.rds.get(k)
                if v is None:
                    continue

                fname = _b64url(k) + ".dump"
                fpath = blocks_dir / fname
                # 原样 bytes 落盘
                fpath.write_bytes(v)

                rec = {
                    "key_b64url": _b64url(k),
                    "file": f"blocks/{fname}",
                    "bytes": len(v),
                }
                mf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                dumped += 1
        return dumped

