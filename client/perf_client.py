#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx


def is_stream(body: Dict[str, Any]) -> bool:
    v = body.get("stream", False)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y")
    return bool(v)


def calc_metrics(trace: Dict[str, int]) -> Dict[str, Optional[int]]:
    def duration(start_key: str, end_key: str) -> Optional[int]:
        if start_key in trace and end_key in trace:
            return int(trace[end_key]) - int(trace[start_key])
        return None

    prepare_queue_wait_ms = duration("proxy_enqueue_ms", "prepare_start_ms")
    prepare_exec_ms = duration("prepare_start_ms", "ready_enqueue_ms")
    ready_queue_wait_ms = duration("ready_enqueue_ms", "ready_dequeue_ms")
    instance_exec_to_first_token_ms = duration("forward_start_ms", "first_token_ms")

    total_prefill_ms = duration("proxy_enqueue_ms", "first_token_ms")
    proxy_before_vllm_ms = duration("proxy_enqueue_ms", "forward_start_ms")
    knowledge_fetch_ms = duration("kdn_fetch_start_ms", "kdn_fetch_end_ms")
    kv_ack_ms = duration("kv_ack_start_ms", "kv_ack_end_ms")

    proxy_queue_wait_ms = None
    if prepare_queue_wait_ms is not None or ready_queue_wait_ms is not None:
        proxy_queue_wait_ms = (prepare_queue_wait_ms or 0) + (ready_queue_wait_ms or 0)

    knowledge_preparation_total_ms = prepare_exec_ms
    vllm_compute_to_first_token_ms = instance_exec_to_first_token_ms

    return {
        "total_prefill_ms": total_prefill_ms,
        "proxy_before_vllm_ms": proxy_before_vllm_ms,

        "prepare_queue_wait_ms": prepare_queue_wait_ms,
        "prepare_exec_ms": prepare_exec_ms,
        "ready_queue_wait_ms": ready_queue_wait_ms,
        "instance_exec_to_first_token_ms": instance_exec_to_first_token_ms,

        "proxy_queue_wait_ms": proxy_queue_wait_ms,
        "knowledge_preparation_total_ms": knowledge_preparation_total_ms,
        "vllm_compute_to_first_token_ms": vllm_compute_to_first_token_ms,

        "knowledge_fetch_ms": knowledge_fetch_ms,
        "kv_ack_ms": kv_ack_ms,
    }


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
    status = resp.status_code

    try:
        obj = resp.json()
    except Exception:
        return status, {"error": "response_not_json", "raw": resp.text[:300]}

    return status, obj.get("_cacheroute_meta", {})


async def run_one(
    client: httpx.AsyncClient,
    req_index: int,
    base_url: str,
    req_tpl: Dict[str, Any],
) -> Dict[str, Any]:
    name = req_tpl.get("name", f"req_{req_index}")
    url_path = req_tpl.get("url_path", "/v1/chat/completions")
    body = dict(req_tpl.get("body", {}))
    url = base_url.rstrip("/") + url_path

    headers = {"Content-Type": "application/json"}
    t0 = time.time()

    if is_stream(body):
        status, meta = await read_chat_stream_meta(client, url, headers, body)
    else:
        status, meta = await read_completions_meta(client, url, headers, body)

    t1 = time.time()

    trace = meta.get("trace", {}) if isinstance(meta, dict) else {}
    metrics = calc_metrics(trace if isinstance(trace, dict) else {})

    return {
        "req_index": req_index,
        "name": name,
        "url_path": url_path,
        "http_status": status,
        "wall_ms": int((t1 - t0) * 1000),
        "metrics": metrics,
        "trace": trace,
        "kv_ack": meta.get("kv_ack", {}) if isinstance(meta, dict) else {},
        "miss_kids": meta.get("miss_kids", []) if isinstance(meta, dict) else [],
        "kv_ready_kids": meta.get("kv_ready_kids", []) if isinstance(meta, dict) else [],
        "text_only_kids": meta.get("text_only_kids", []) if isinstance(meta, dict) else [],
        "error": meta.get("error") if isinstance(meta, dict) else None,
        "injection_type": body.get("Injection_type"),
        "rag": body.get("RAG"),
        "stream": body.get("stream"),
    }


def summarize(results: List[Dict[str, Any]]) -> None:
    def fmt(v: Any) -> str:
        return "N/A" if v is None else str(v)

    print("\n" + "=" * 120)
    print("Compact Request Performance Summary")
    print("=" * 120)
    print(
        "idx | name | injection | status | total_prefill_ms | "
        "proxy_before_vllm_ms | proxy_queue_wait_ms | knowledge_fetch_ms | "
        "knowledge_preparation_total_ms | vllm_compute_to_first_token_ms | error"
    )
    print("-" * 120)

    for r in results:
        m = r["metrics"]
        print(
            f"{r['req_index']:03d} | "
            f"{r['name']} | "
            f"{r['injection_type']} | "
            f"{r['http_status']} | "
            f"{fmt(m.get('total_prefill_ms'))} | "
            f"{fmt(m.get('proxy_before_vllm_ms'))} | "
            f"{fmt(m.get('proxy_queue_wait_ms'))} | "
            f"{fmt(m.get('knowledge_fetch_ms'))} | "
            f"{fmt(m.get('knowledge_preparation_total_ms'))} | "
            f"{fmt(m.get('vllm_compute_to_first_token_ms'))} | "
            f"{r.get('error')}"
        )

    print("\n" + "=" * 120)
    print("Average Performance Summary")
    print("=" * 120)

    metric_names = [
        ("total_prefill_ms", "Average total prefill time"),
        ("proxy_before_vllm_ms", "Average time inside proxy before vLLM"),
        ("proxy_queue_wait_ms", "Average queue waiting time inside proxy"),
        ("knowledge_fetch_ms", "Average knowledge fetch time"),
        ("knowledge_preparation_total_ms", "Average total knowledge preparation time"),
        ("vllm_compute_to_first_token_ms", "Average actual vLLM compute time to first token"),
    ]

    for metric_key, metric_label in metric_names:
        vals = []
        for r in results:
            v = r["metrics"].get(metric_key)
            if isinstance(v, int):
                vals.append(v)

        if vals:
            print(f"{metric_label}: {int(statistics.mean(vals))} ms")

    print("=" * 120)


def build_selected_templates(
    req_templates: List[Dict[str, Any]],
    total_requests: int,
    allow_duplicate: bool,
) -> List[Dict[str, Any]]:
    if allow_duplicate:
        return [random.choice(req_templates) for _ in range(total_requests)]

    if total_requests > len(req_templates):
        raise ValueError(
            f"requests={total_requests} is larger than workload size={len(req_templates)} "
            f"when allow_duplicate is disabled"
        )

    return random.sample(req_templates, total_requests)


async def main_async(args: argparse.Namespace) -> None:
    workload = json.loads(Path(args.workload_file).read_text(encoding="utf-8"))
    req_templates = workload.get("requests", [])

    if not isinstance(req_templates, list) or not req_templates:
        raise ValueError("workload_file must contain a non-empty 'requests' list")

    timeout = httpx.Timeout(300.0)

    if args.seed is not None:
        random.seed(args.seed)

    selected_templates = build_selected_templates(
        req_templates=req_templates,
        total_requests=args.requests,
        allow_duplicate=args.allow_duplicate,
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        sem = asyncio.Semaphore(args.concurrency)

        async def bounded_run(i: int, tpl: Dict[str, Any]):
            async with sem:
                return await run_one(client, i, args.base_url, tpl)

        tasks = [
            asyncio.create_task(bounded_run(i, tpl))
            for i, tpl in enumerate(selected_templates)
        ]
        results = await asyncio.gather(*tasks)

    summarize(results)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="scheduler base url, e.g. http://127.0.0.1:7001")
    parser.add_argument("--workload-file", required=True, help="JSON file containing multiple request templates")
    parser.add_argument("--requests", type=int, default=10, help="total requests")
    parser.add_argument("--concurrency", type=int, default=4, help="concurrent requests")
    parser.add_argument("--allow-duplicate", action="store_true", help="allow selecting the same request template multiple times")
    parser.add_argument("--seed", type=int, default=None,  help="random seed for reproducible sampling")
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()