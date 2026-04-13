#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

# 允许在 "python client/kv_timing_sender.py" 时导入仓库模块
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from proxy.metrics.queue_predictor import queue_predictor


def parse_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    if isinstance(v, (int, float)):
        return bool(v)
    return False


def resolve_injection_type(base_injection_type: str, req_index: int) -> str:
    if base_injection_type == "hybrid":
        return "kvcache" if (req_index % 3 in (0, 1)) else "text"
    return base_injection_type


def is_stream(body: Dict[str, Any]) -> bool:
    return parse_bool(body.get("stream", False))


def duration(trace: Dict[str, Any], start_key: str, end_key: str) -> Optional[int]:
    s = trace.get(start_key)
    e = trace.get(end_key)
    if isinstance(s, int) and isinstance(e, int):
        return int(e) - int(s)
    return None


async def read_chat_stream_meta(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
) -> Tuple[int, Dict[str, Any]]:
    meta: Dict[str, Any] = {}

    async with client.stream("POST", url, headers=headers, json=body) as resp:
        status = resp.status_code
        current_event = "message"

        async for line in resp.aiter_lines():
            if line is None:
                continue
            if not line:
                current_event = "message"
                continue
            if line.startswith("event:"):
                current_event = line[len("event:"):].strip()
                continue
            if line.startswith("data:"):
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                if current_event == "cacheroute_meta":
                    try:
                        meta = json.loads(data)
                    except Exception:
                        meta = {"raw_meta": data}
        return status, meta


async def read_completions_meta(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
) -> Tuple[int, Dict[str, Any]]:
    resp = await client.post(url, headers=headers, json=body)
    try:
        obj = resp.json()
    except Exception:
        return resp.status_code, {"error": "response_not_json", "raw": resp.text[:300]}
    return resp.status_code, obj.get("_cacheroute_meta", {})


def build_global_request_defaults(args: argparse.Namespace) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": args.model,
        "stream": parse_bool(args.stream),
        "RAG": parse_bool(args.rag),
        "Injection_type": args.injection_type,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "top_p": args.top_p,
    }
    if args.knowledge_id:
        body["knowledge_id"] = args.knowledge_id
    if args.knowledge_ids:
        body["knowledge_ids"] = [x.strip() for x in args.knowledge_ids.split(",") if x.strip()]
    return body


def normalize_request_template(req_tpl: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    name = req_tpl.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("each request template must contain non-empty 'name'")

    messages = req_tpl.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"request '{name}' must contain non-empty 'messages'")

    url_path = req_tpl.get("url_path", args.url_path)
    body = build_global_request_defaults(args)
    body["messages"] = messages

    for key in [
        "model", "stream", "RAG", "Injection_type", "max_tokens", "temperature", "top_p",
        "knowledge_id", "knowledge_ids",
        # 允许 workload 透传长度元数据给统计器
        "knowledge_length_tokens", "task_length_tokens", "header_overhead_tokens", "total_length_tokens",
    ]:
        if key in req_tpl:
            body[key] = req_tpl[key]

    body["stream"] = parse_bool(body.get("stream"))
    body["RAG"] = parse_bool(body.get("RAG"))
    if "knowledge_ids" in body and isinstance(body["knowledge_ids"], str):
        body["knowledge_ids"] = [x.strip() for x in body["knowledge_ids"].split(",") if x.strip()]

    return {"name": name, "url_path": url_path, "body": body}


def build_selected_templates(req_templates: List[Dict[str, Any]], total_requests: int, allow_duplicate: bool) -> List[Dict[str, Any]]:
    if allow_duplicate:
        return [random.choice(req_templates) for _ in range(total_requests)]
    if total_requests > len(req_templates):
        raise ValueError(
            f"requests={total_requests} is larger than workload size={len(req_templates)} when --allow-duplicate is disabled"
        )
    return random.sample(req_templates, total_requests)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def build_kv_timing_record(req_index: int, req_name: str, body: Dict[str, Any], status: int, meta: Dict[str, Any], token_kv_gb: float) -> Dict[str, Any]:
    trace = meta.get("trace", {}) if isinstance(meta, dict) else {}
    trace = trace if isinstance(trace, dict) else {}
    kv_ack = meta.get("kv_ack", {}) if isinstance(meta, dict) else {}
    kv_ack = kv_ack if isinstance(kv_ack, dict) else {}

    total_length_tokens = _safe_int(
        trace.get("predict_length_tokens", body.get("total_length_tokens", 0)),
        default=0,
    )

    knowledge_length_tokens = _safe_int(body.get("knowledge_length_tokens", 0), default=0)
    if knowledge_length_tokens <= 0:
        # 兜底：若 workload 未提供 knowledge_length_tokens，则按 total 估算（会把问题+首部也算进去）
        knowledge_length_tokens = total_length_tokens

    actual_hit_length_tokens = (knowledge_length_tokens // 256) * 256
    remaining_compute_tokens = max(0, total_length_tokens - actual_hit_length_tokens)

    queue_wait_ms = trace.get("actual_wait_ms")
    compute_ms = trace.get("actual_compute_ms")
    total_ms = trace.get("actual_total_ms")

    text_compute_estimate_ms: Optional[float] = None
    if remaining_compute_tokens > 0:
        text_compute_estimate_ms = float(queue_predictor(length=max(1, remaining_compute_tokens), bs=1) * 1000.0)
    else:
        text_compute_estimate_ms = 0.0

    lmcache_redis_pull_ms: Optional[float] = None
    if isinstance(compute_ms, int) and text_compute_estimate_ms is not None:
        lmcache_redis_pull_ms = float(compute_ms) - float(text_compute_estimate_ms)

    req_id = trace.get("request_id")
    if req_id is None:
        req_id = kv_ack.get("request_id")
    if req_id is None:
        req_id = req_index

    record = {
        "request_id": req_id,
        "req_index": req_index,
        "name": req_name,
        "http_status": status,
        "injection_type": body.get("Injection_type"),
        # 用户要求字段
        "total_length_tokens": total_length_tokens,
        "actual_hit_length_tokens": actual_hit_length_tokens,
        "remaining_compute_tokens": remaining_compute_tokens,
        "kvcache_size_gb": round(float(actual_hit_length_tokens) * float(token_kv_gb), 8),
        "queue_wait_ms": queue_wait_ms,
        "compute_ms": compute_ms,
        "text_compute_estimate_ms": round(text_compute_estimate_ms, 3) if text_compute_estimate_ms is not None else None,
        "lmcache_redis_pull_ms": round(lmcache_redis_pull_ms, 3) if lmcache_redis_pull_ms is not None else None,
        "total_ms": total_ms,
        # 补充关键建模字段
        "predict_total_ms": trace.get("predict_total_ms"),
        "predict_queue_wait_ms": trace.get("predict_queue_wait_ms"),
        "predict_compute_ms": trace.get("predict_compute_ms"),
        "predict_know_prepare_ms": trace.get("predict_know_prepare_ms"),
        "knowledge_fetch_ms": duration(trace, "kdn_fetch_start_ms", "kdn_fetch_end_ms"),
        "kv_ack_ms": duration(trace, "kv_ack_start_ms", "kv_ack_end_ms"),
        "payload_bytes": kv_ack.get("payload_bytes"),
        "payload_files": kv_ack.get("payload_files"),
        "keys_injected": kv_ack.get("keys_injected"),
        "injected_kids": kv_ack.get("injected_kids", []),
        "text_only_kids": kv_ack.get("text_only_kids", []),
        "miss_kids": kv_ack.get("miss_kids", []),
        "kv_ready_kids": meta.get("kv_ready_kids", []),
        "error": meta.get("error"),
    }

    return record


async def run_one(
    client: httpx.AsyncClient,
    req_index: int,
    base_url: str,
    req_tpl: Dict[str, Any],
    scheduled_send_ts: Optional[float],
    token_kv_gb: float,
) -> Dict[str, Any]:
    name = req_tpl.get("name", f"req_{req_index}")
    url_path = req_tpl.get("url_path", "/v1/chat/completions")
    body = dict(req_tpl.get("body", {}))

    body["Injection_type"] = resolve_injection_type(str(body.get("Injection_type", "text")), req_index)
    url = base_url.rstrip("/") + url_path
    headers = {"Content-Type": "application/json"}

    actual_send_ts = time.time()
    if is_stream(body):
        status, meta = await read_chat_stream_meta(client, url, headers, body)
    else:
        status, meta = await read_completions_meta(client, url, headers, body)

    record = build_kv_timing_record(
        req_index=req_index,
        req_name=name,
        body=body,
        status=status,
        meta=meta if isinstance(meta, dict) else {},
        token_kv_gb=token_kv_gb,
    )

    if scheduled_send_ts is not None:
        record["client_send_delay_ms"] = int((actual_send_ts - scheduled_send_ts) * 1000)
    return record


def print_summary(rows: List[Dict[str, Any]], total_elapsed_s: float, target_rps: float, peak_inflight: int) -> None:
    print("\n" + "=" * 180)
    print("KVCache Injection Timing Summary")
    print("=" * 180)
    print(
        "idx | request_id | status | inj | total_len | hit_len | remain_len | kvcache_gb | "
        "queue_wait_ms | compute_ms | text_est_ms | redis_pull_ms | total_ms | kv_ack_ms | payload_bytes | error"
    )
    print("-" * 180)

    for r in rows:
        print(
            f"{r.get('req_index')} | {r.get('request_id')} | {r.get('http_status')} | {r.get('injection_type')} | "
            f"{r.get('total_length_tokens')} | {r.get('actual_hit_length_tokens')} | {r.get('remaining_compute_tokens')} | "
            f"{r.get('kvcache_size_gb')} | {r.get('queue_wait_ms')} | {r.get('compute_ms')} | "
            f"{r.get('text_compute_estimate_ms')} | {r.get('lmcache_redis_pull_ms')} | {r.get('total_ms')} | "
            f"{r.get('kv_ack_ms')} | {r.get('payload_bytes')} | {r.get('error')}"
        )

    def avg_of(key: str) -> Optional[float]:
        vals = [float(r[key]) for r in rows if isinstance(r.get(key), (int, float))]
        if not vals:
            return None
        return float(statistics.mean(vals))

    print("\nAggregate:")
    for k in [
        "queue_wait_ms", "compute_ms", "text_compute_estimate_ms", "lmcache_redis_pull_ms",
        "total_ms", "kv_ack_ms", "knowledge_fetch_ms", "predict_total_ms",
    ]:
        v = avg_of(k)
        if v is not None:
            print(f"  avg_{k}: {v:.3f}")

    print(f"  requests: {len(rows)}")
    print(f"  target_rps: {target_rps}")
    print(f"  elapsed_s: {total_elapsed_s:.3f}")
    print(f"  actual_throughput_rps: {(len(rows) / total_elapsed_s):.3f}" if total_elapsed_s > 0 else "  actual_throughput_rps: N/A")
    print(f"  peak_inflight: {peak_inflight}")
    print("=" * 180)


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    # 扁平化 list 字段，方便 csv 消费
    normalized: List[Dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        for k in ["injected_kids", "text_only_kids", "miss_kids", "kv_ready_kids"]:
            if isinstance(item.get(k), list):
                item[k] = ",".join([str(x) for x in item[k]])
        normalized.append(item)

    fields: List[str] = []
    seen = set()
    for r in normalized:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                fields.append(k)

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(normalized)


async def main_async(args: argparse.Namespace) -> None:
    if args.requests <= 0:
        raise ValueError("--requests must be > 0")
    if args.rps <= 0:
        raise ValueError("--rps must be > 0")

    workload = json.loads(Path(args.workload_file).read_text(encoding="utf-8"))
    req_templates = workload.get("requests", [])
    if not isinstance(req_templates, list) or not req_templates:
        raise ValueError("workload_file must contain a non-empty 'requests' list")

    if args.seed is not None:
        random.seed(args.seed)

    normalized_templates = [normalize_request_template(tpl, args) for tpl in req_templates]
    selected_templates = build_selected_templates(normalized_templates, args.requests, args.allow_duplicate)

    timeout = httpx.Timeout(args.timeout_s)
    interval = 1.0 / float(args.rps)

    inflight = 0
    peak_inflight = 0
    inflight_lock = asyncio.Lock()
    tasks: List[asyncio.Task] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        t_start = time.time()

        async def fire_one(i: int, tpl: Dict[str, Any], scheduled_ts: float) -> Dict[str, Any]:
            nonlocal inflight, peak_inflight
            async with inflight_lock:
                inflight += 1
                peak_inflight = max(peak_inflight, inflight)
            try:
                return await run_one(
                    client=client,
                    req_index=i,
                    base_url=args.base_url,
                    req_tpl=tpl,
                    scheduled_send_ts=scheduled_ts,
                    token_kv_gb=args.kv_gb_per_token,
                )
            finally:
                async with inflight_lock:
                    inflight -= 1

        for i, tpl in enumerate(selected_templates):
            scheduled_ts = t_start + i * interval
            delay = scheduled_ts - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(fire_one(i, tpl, scheduled_ts)))

        rows = await asyncio.gather(*tasks)
        t_end = time.time()

    print_summary(rows, total_elapsed_s=(t_end - t_start), target_rps=args.rps, peak_inflight=peak_inflight)

    if args.output_jsonl:
        out = Path(args.output_jsonl)
        out.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(out, rows)
        print(f"[output] jsonl written: {out}")

    if args.output_csv:
        out = Path(args.output_csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        write_csv(out, rows)
        print(f"[output] csv written: {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="RPS task sender + KVCache timing statistics collector")
    ap.add_argument("--base-url", required=True, help="scheduler base url, e.g. http://127.0.0.1:7001")
    ap.add_argument("--workload-file", required=True, help="workload json path")
    ap.add_argument("--requests", type=int, required=True, help="total requests")
    ap.add_argument("--rps", type=float, required=True, help="target requests per second")
    ap.add_argument("--allow-duplicate", action="store_true", help="allow duplicate request templates")
    ap.add_argument("--seed", type=int, default=None)

    ap.add_argument("--url-path", default="/v1/chat/completions")
    ap.add_argument("--model", required=True)
    ap.add_argument("--stream", type=str, default="true")
    ap.add_argument("--rag", type=str, default="true")
    ap.add_argument("--injection-type", choices=["text", "kvcache", "hybrid"], default="kvcache")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--top-p", type=float, default=1.0)
    ap.add_argument("--knowledge-id", default=None)
    ap.add_argument("--knowledge-ids", default=None)

    ap.add_argument("--kv-gb-per-token", type=float, default=0.0000381, help="KVCache size(GB) per token")
    ap.add_argument("--timeout-s", type=float, default=300.0)
    ap.add_argument("--output-jsonl", default=None)
    ap.add_argument("--output-csv", default=None)

    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
