#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import random
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

TRACE_ORDER_TOLERANCE_MS = 2


def parse_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    if isinstance(v, (int, float)):
        return bool(v)
    return False


def parse_hybrid_pattern(pattern: str) -> Tuple[int, int]:
    if not isinstance(pattern, str):
        raise ValueError("--hybrid-pattern must be a string in A:B format, e.g. 2:1")
    parts = pattern.split(":")
    if len(parts) != 2:
        raise ValueError(f"--hybrid-pattern '{pattern}' is invalid, expected format A:B (e.g. 2:1)")
    left, right = parts[0].strip(), parts[1].strip()
    if not left.isdigit() or not right.isdigit():
        raise ValueError(f"--hybrid-pattern '{pattern}' is invalid, A and B must be positive integers")
    a = int(left)
    b = int(right)
    if a <= 0 or b <= 0:
        raise ValueError(f"--hybrid-pattern '{pattern}' is invalid, A and B must be > 0")
    return a, b


def parse_gpu_ids(gpu_ids_str: Optional[str]) -> Optional[Set[int]]:
    if gpu_ids_str is None:
        return None
    raw = str(gpu_ids_str).strip()
    if not raw:
        return None
    ids: Set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            raise ValueError(f"--gpu-ids '{gpu_ids_str}' is invalid, empty GPU id found")
        if not token.isdigit():
            raise ValueError(f"--gpu-ids '{gpu_ids_str}' is invalid, '{token}' is not a non-negative integer")
        ids.add(int(token))
    return ids


def resolve_injection_type(base_injection_type: str, req_index: int, hybrid_pattern: str) -> str:
    if base_injection_type == "hybrid":
        kv_count, text_count = parse_hybrid_pattern(hybrid_pattern)
        cycle = kv_count + text_count
        pos = req_index % cycle
        if pos < kv_count:
            return "kvcache"
        return "text"
    return base_injection_type


def is_stream(body: Dict[str, Any]) -> bool:
    return parse_bool(body.get("stream", False))


def check_trace_order(trace: Dict[str, int]) -> List[str]:
    warnings: List[str] = []
    ordered_pairs = [
        ("proxy_recv_ms", "proxy_enqueue_ms"),
        ("prepare_queue_enqueue_ms", "prepare_dequeue_ms"),
        ("prepare_dequeue_ms", "prepare_start_ms"),
        ("kdn_fetch_start_ms", "kdn_fetch_end_ms"),
        ("kv_inject_queue_enqueue_ms", "kv_inject_start_ms"),
        ("kv_inject_start_ms", "kv_ack_start_ms"),
        ("kv_ack_start_ms", "kv_ack_end_ms"),
        ("kv_ack_end_ms", "kv_inject_end_ms"),
        ("text_prefill_build_start_ms", "text_prefill_build_end_ms"),
        ("ready_enqueue_ms", "ready_dequeue_ms"),
        ("ready_dequeue_ms", "forward_wait_start_ms"),
        ("forward_wait_start_ms", "forward_wait_end_ms"),
        ("forward_wait_end_ms", "forward_start_ms"),
        ("forward_start_ms", "first_token_ms"),
    ]
    for left_key, right_key in ordered_pairs:
        if left_key not in trace or right_key not in trace:
            continue
        left_val = int(trace[left_key])
        right_val = int(trace[right_key])
        if right_val + TRACE_ORDER_TOLERANCE_MS < left_val:
            warnings.append(
                f"trace timestamp order violation: {left_key}={left_val} > {right_key}={right_val}"
            )

    return warnings


async def monitor_gpu(
    stop_event: asyncio.Event,
    sample_interval_s: float,
    gpu_ids: Optional[Set[int]],
) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    warned_unavailable = False
    nvidia_smi_path = shutil.which("nvidia-smi")
    if not nvidia_smi_path:
        print("[GPU] nvidia-smi not available, skip GPU monitoring")
        return samples

    cmd = [
        nvidia_smi_path,
        "--query-gpu=index,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw",
        "--format=csv,noheader,nounits",
    ]

    while not stop_event.is_set():
        ts = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await proc.communicate()
            if proc.returncode != 0:
                if not warned_unavailable:
                    err_msg = err_b.decode("utf-8", errors="replace").strip()
                    print(f"[GPU] nvidia-smi execution failed, skip GPU monitoring: {err_msg or 'unknown error'}")
                    warned_unavailable = True
                break
            lines = out_b.decode("utf-8", errors="replace").strip().splitlines()
            for line in lines:
                cols = [x.strip() for x in line.split(",")]
                if len(cols) != 6:
                    continue
                try:
                    gpu_index = int(cols[0])
                    if gpu_ids is not None and gpu_index not in gpu_ids:
                        continue
                    samples.append({
                        "timestamp": ts,
                        "gpu_index": gpu_index,
                        "gpu_util": int(cols[1]),
                        "mem_util": int(cols[2]),
                        "mem_used_mb": int(cols[3]),
                        "mem_total_mb": int(cols[4]),
                        "power_w": float(cols[5]),
                    })
                except Exception:
                    continue
        except FileNotFoundError:
            if not warned_unavailable:
                print("[GPU] nvidia-smi not available, skip GPU monitoring")
                warned_unavailable = True
            break
        except Exception as e:
            if not warned_unavailable:
                print(f"[GPU] nvidia-smi execution failed, skip GPU monitoring: {e}")
                warned_unavailable = True
            break

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sample_interval_s)
        except asyncio.TimeoutError:
            pass

    return samples


def summarize_gpu(
    samples: List[Dict[str, Any]],
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
) -> None:
    print("\n" + "=" * 160)
    print("GPU Utilization Summary")
    print("=" * 160)
    filtered_samples = samples
    if start_ts is not None and end_ts is not None:
        filtered_samples = [
            s for s in samples
            if isinstance(s.get("timestamp"), (int, float))
            and start_ts <= float(s["timestamp"]) <= end_ts
        ]

    if not filtered_samples:
        if start_ts is not None and end_ts is not None:
            print("GPU monitoring enabled but no valid samples collected in experiment window.")
        else:
            print("GPU monitoring enabled but no valid samples collected.")
        print("=" * 160)
        return

    by_gpu: Dict[int, List[Dict[str, Any]]] = {}
    for s in filtered_samples:
        gid = int(s.get("gpu_index", -1))
        by_gpu.setdefault(gid, []).append(s)

    overall_utils: List[float] = []
    for gid in sorted(by_gpu.keys()):
        items = by_gpu[gid]
        gpu_utils = [float(x["gpu_util"]) for x in items]
        mem_utils = [float(x["mem_util"]) for x in items]
        mem_used = [float(x["mem_used_mb"]) for x in items]
        mem_total_vals = [float(x["mem_total_mb"]) for x in items]
        power_vals = [float(x["power_w"]) for x in items]
        overall_utils.extend(gpu_utils)
        mem_total_mb = int(statistics.mean(mem_total_vals)) if mem_total_vals else 0
        print(
            f"GPU {gid}: samples={len(items)} "
            f"avg_gpu_util={statistics.mean(gpu_utils):.1f}% max_gpu_util={int(max(gpu_utils))}% "
            f"avg_mem_util={statistics.mean(mem_utils):.1f}% "
            f"avg_mem_used={statistics.mean(mem_used):.1f}MB max_mem_used={int(max(mem_used))}MB "
            f"total_mem={mem_total_mb}MB avg_power={statistics.mean(power_vals):.1f}W"
        )

    print(f"Overall average GPU utilization: {statistics.mean(overall_utils):.1f}%")
    print("=" * 160)


def calc_metrics(trace: Dict[str, int]) -> Dict[str, Optional[int]]:
    def duration(start_key: str, end_key: str) -> Optional[int]:
        if start_key in trace and end_key in trace:
            return int(trace[end_key]) - int(trace[start_key])
        return None

    proxy_admission_ms = duration("proxy_recv_ms", "proxy_enqueue_ms")
    route_select_ms = duration("route_select_start_ms", "route_select_end_ms")
    prepare_queue_wait_ms = duration("prepare_queue_enqueue_ms", "prepare_dequeue_ms")
    if prepare_queue_wait_ms is None:
        prepare_queue_wait_ms = duration("proxy_enqueue_ms", "prepare_start_ms")
    prepare_worker_gap_ms = duration("prepare_dequeue_ms", "prepare_start_ms")
    prepare_exec_ms = duration("prepare_start_ms", "ready_enqueue_ms")
    prepare_total_ms = duration("prepare_start_ms", "ready_enqueue_ms")
    ready_queue_wait_ms = duration("ready_enqueue_ms", "ready_dequeue_ms")
    forward_wait_ms = duration("forward_wait_start_ms", "forward_wait_end_ms")
    ready_dequeue_to_forward_ms = duration("ready_dequeue_ms", "forward_start_ms")
    vllm_first_token_ms = duration("forward_start_ms", "first_token_ms")
    instance_exec_to_first_token_ms = vllm_first_token_ms

    total_prefill_ms = duration("proxy_enqueue_ms", "first_token_ms")
    proxy_before_vllm_ms = duration("proxy_enqueue_ms", "forward_start_ms")
    proxy_recv_to_forward_ms = duration("proxy_recv_ms", "forward_start_ms")
    knowledge_fetch_ms = duration("kdn_fetch_start_ms", "kdn_fetch_end_ms")
    kdn_fetch_ms = knowledge_fetch_ms
    kv_ack_ms = duration("kv_ack_start_ms", "kv_ack_end_ms")
    kv_inject_queue_wait_ms = duration("kv_inject_queue_enqueue_ms", "kv_inject_start_ms")
    kv_inject_exec_ms = duration("kv_inject_start_ms", "kv_inject_end_ms")
    text_prefill_build_ms = duration("text_prefill_build_start_ms", "text_prefill_build_end_ms")

    proxy_queue_wait_ms = None
    if prepare_queue_wait_ms is not None or ready_queue_wait_ms is not None:
        proxy_queue_wait_ms = (prepare_queue_wait_ms or 0) + (ready_queue_wait_ms or 0)
    proxy_wait_until_forward_ms = None
    if (
        prepare_queue_wait_ms is not None
        or ready_queue_wait_ms is not None
        or ready_dequeue_to_forward_ms is not None
    ):
        proxy_wait_until_forward_ms = (
            (prepare_queue_wait_ms or 0)
            + (ready_queue_wait_ms or 0)
            + (ready_dequeue_to_forward_ms or 0)
        )
    proxy_enqueue_to_forward_ms = duration("proxy_enqueue_ms", "forward_start_ms")

    knowledge_preparation_total_ms = prepare_exec_ms
    vllm_compute_to_first_token_ms = instance_exec_to_first_token_ms

    return {
        # 兼容旧展示口径
        "total_prefill_ms": total_prefill_ms,
        "proxy_before_vllm_ms": proxy_before_vllm_ms,
        "proxy_queue_wait_ms": proxy_queue_wait_ms,
        "knowledge_fetch_ms": knowledge_fetch_ms,
        "knowledge_preparation_total_ms": knowledge_preparation_total_ms,
        "vllm_compute_to_first_token_ms": vllm_compute_to_first_token_ms,
        "proxy_wait_until_forward_ms": proxy_wait_until_forward_ms,
        "proxy_enqueue_to_forward_ms": proxy_enqueue_to_forward_ms,
        "proxy_recv_to_forward_ms": proxy_recv_to_forward_ms,
        "proxy_admission_ms": proxy_admission_ms,
        "route_select_ms": route_select_ms,

        # 新拆细口径
        "prepare_queue_wait_ms": prepare_queue_wait_ms,
        "prepare_worker_gap_ms": prepare_worker_gap_ms,
        "prepare_exec_ms": prepare_exec_ms,
        "prepare_total_ms": prepare_total_ms,
        "ready_queue_wait_ms": ready_queue_wait_ms,
        "forward_wait_ms": forward_wait_ms,
        "ready_dequeue_to_forward_ms": ready_dequeue_to_forward_ms,
        "kdn_fetch_ms": kdn_fetch_ms,
        "kv_inject_queue_wait_ms": kv_inject_queue_wait_ms,
        "kv_inject_exec_ms": kv_inject_exec_ms,
        "text_prefill_build_ms": text_prefill_build_ms,
        "vllm_first_token_ms": vllm_first_token_ms,
        "instance_exec_to_first_token_ms": instance_exec_to_first_token_ms,
        "kv_ack_ms": kv_ack_ms,
        "actual_prepare_total_ms": trace.get("actual_prepare_total_ms"),
        "prepare_buffer_wait_ms": trace.get("prepare_buffer_wait_ms"),
        "actual_ready_queue_ms": trace.get("actual_ready_queue_ms"),
        "actual_vllm_internal_ms": trace.get("actual_vllm_internal_ms"),
        "predict_queue_wait_ms": trace.get("predict_queue_wait_ms"),
        "predict_error_ms": trace.get("predict_error_ms"),
    }


def check_trace_integrity(trace: Dict[str, int], injection_type: str) -> List[str]:
    warnings: List[str] = []
    mode = str(injection_type or "").strip().lower()
    if mode == "kvcache":
        path = str(trace.get("kvcache_actual_path", "") or "")
        if path == "kv_inject":
            required_pairs = [
                ("kdn_fetch_start_ms", "kdn_fetch_end_ms"),
                ("kv_ack_start_ms", "kv_ack_end_ms"),
                ("kv_inject_start_ms", "kv_inject_end_ms"),
            ]
            for sk, ek in required_pairs:
                if sk not in trace or ek not in trace:
                    warnings.append(f"kvcache trace missing {sk}/{ek}")
    if mode == "text":
        path = str(trace.get("text_actual_path", "") or "")
        if path == "text_inject":
            if "text_prefill_build_start_ms" not in trace or "text_prefill_build_end_ms" not in trace:
                warnings.append("text trace missing text_prefill_build_start_ms/text_prefill_build_end_ms")
            if "prepare_start_ms" not in trace or "ready_enqueue_ms" not in trace:
                warnings.append("text trace missing prepare_start_ms/ready_enqueue_ms")
    return warnings


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

        if not meta:
            return status, {"error": "missing_cacheroute_meta"}
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


def normalize_request_template(
    req_tpl: Dict[str, Any],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    if not isinstance(req_tpl, dict):
        raise ValueError("each request template must be a JSON object")

    name = req_tpl.get("name")
    if not name:
        raise ValueError("each request template must contain a non-empty 'name'")

    messages = req_tpl.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"request '{name}' must contain a non-empty 'messages' list")

    url_path = req_tpl.get("url_path", args.url_path)

    body = build_global_request_defaults(args)
    body["messages"] = messages

    override_keys = [
        "model",
        "stream",
        "RAG",
        "Injection_type",
        "max_tokens",
        "temperature",
        "top_p",
        "knowledge_id",
        "knowledge_ids",
    ]
    for key in override_keys:
        if key in req_tpl:
            body[key] = req_tpl[key]

    body["stream"] = parse_bool(body.get("stream"))
    body["RAG"] = parse_bool(body.get("RAG"))

    if "knowledge_ids" in body and isinstance(body["knowledge_ids"], str):
        body["knowledge_ids"] = [x.strip() for x in body["knowledge_ids"].split(",") if x.strip()]

    return {
        "name": name,
        "url_path": url_path,
        "body": body,
    }


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


async def run_one(
    client: httpx.AsyncClient,
    req_index: int,
    base_url: str,
    req_tpl: Dict[str, Any],
    hybrid_pattern: str,
    scheduled_send_ts: Optional[float] = None,
) -> Dict[str, Any]:
    name = req_tpl.get("name", f"req_{req_index}")
    url_path = req_tpl.get("url_path", "/v1/chat/completions")
    body = dict(req_tpl.get("body", {}))

    actual_injection_type = resolve_injection_type(
        str(body.get("Injection_type", "text")),
        req_index,
        hybrid_pattern,
    )
    body["Injection_type"] = actual_injection_type

    url = base_url.rstrip("/") + url_path

    headers = {"Content-Type": "application/json"}

    actual_send_ts = time.time()
    t0 = actual_send_ts

    if is_stream(body):
        status, meta = await read_chat_stream_meta(client, url, headers, body)
    else:
        status, meta = await read_completions_meta(client, url, headers, body)

    t1 = time.time()

    trace = meta.get("trace", {}) if isinstance(meta, dict) else {}
    metrics = calc_metrics(trace if isinstance(trace, dict) else {})
    trace_warnings = check_trace_order(trace if isinstance(trace, dict) else {})
    trace_warnings.extend(check_trace_integrity(trace if isinstance(trace, dict) else {}, actual_injection_type))

    meta_missing = False
    if isinstance(meta, dict):
        meta_missing = bool(meta.get("error") == "missing_cacheroute_meta" or not meta)
    else:
        meta_missing = True
    trace_key_count = len(trace) if isinstance(trace, dict) else 0
    trace_has_first_token = bool(isinstance(trace, dict) and "first_token_ms" in trace)
    trace_has_forward_start = bool(isinstance(trace, dict) and "forward_start_ms" in trace)

    client_send_delay_ms: Optional[int] = None
    if scheduled_send_ts is not None:
        client_send_delay_ms = int((actual_send_ts - scheduled_send_ts) * 1000)

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
        "injection_type": actual_injection_type,
        "trace_warnings": trace_warnings,
        "meta_missing": meta_missing,
        "trace_key_count": trace_key_count,
        "trace_has_first_token": trace_has_first_token,
        "trace_has_forward_start": trace_has_forward_start,
        "rag": body.get("RAG"),
        "stream": body.get("stream"),
        "request_body": body,
        "scheduled_send_ts": scheduled_send_ts,
        "actual_send_ts": actual_send_ts,
        "client_send_delay_ms": client_send_delay_ms,
    }


def summarize(
    results: List[Dict[str, Any]],
    mode: str,
    total_elapsed_s: float,
    peak_inflight: int,
    target_rps: Optional[float] = None,
    print_trace: bool = False,
    hybrid_pattern: str = "2:1",
    gpu_samples: Optional[List[Dict[str, Any]]] = None,
    gpu_monitor_enabled: bool = False,
    exp_start_ts: Optional[float] = None,
    exp_end_ts: Optional[float] = None,
) -> None:
    def fmt(v: Any) -> str:
        return "N/A" if v is None else str(v)

    def collect_metric(metric_key: str) -> List[int]:
        vals: List[int] = []
        for r in results:
            v = r["metrics"].get(metric_key)
            if isinstance(v, int):
                vals.append(v)
        return vals

    print("\n" + "=" * 160)
    print("Compact Request Performance Summary")
    print("=" * 160)
    print(
        "idx | name | injection | server_injection_mode | text_actual_path | kvcache_actual_path | status | total_prefill_ms | "
        "proxy_before_vllm_ms | proxy_queue_wait_ms | ready_dequeue_to_forward_ms | "
        "proxy_wait_until_forward_ms | proxy_enqueue_to_forward_ms | proxy_recv_to_forward_ms | "
        "prepare_queue_wait_ms | prepare_worker_gap_ms | kdn_fetch_ms | kv_ack_ms | "
        "kv_inject_queue_wait_ms | kv_inject_exec_ms | text_prefill_build_ms | "
        "ready_queue_wait_ms | forward_wait_ms | knowledge_fetch_ms | "
        "knowledge_preparation_total_ms | vllm_compute_to_first_token_ms | "
        "client_send_delay_ms | wall_ms | error"
    )
    print("-" * 160)

    for r in results:
        m = r["metrics"]
        t = r.get("trace", {}) if isinstance(r.get("trace"), dict) else {}
        print(
            f"{r['req_index']:03d} | "
            f"{r['name']} | "
            f"{r['injection_type']} | "
            f"{fmt(t.get('injection_mode'))} | "
            f"{fmt(t.get('text_actual_path'))} | "
            f"{fmt(t.get('kvcache_actual_path'))} | "
            f"{r['http_status']} | "
            f"{fmt(m.get('total_prefill_ms'))} | "
            f"{fmt(m.get('proxy_before_vllm_ms'))} | "
            f"{fmt(m.get('proxy_queue_wait_ms'))} | "
            f"{fmt(m.get('ready_dequeue_to_forward_ms'))} | "
            f"{fmt(m.get('proxy_wait_until_forward_ms'))} | "
            f"{fmt(m.get('proxy_enqueue_to_forward_ms'))} | "
            f"{fmt(m.get('proxy_recv_to_forward_ms'))} | "
            f"{fmt(m.get('prepare_queue_wait_ms'))} | "
            f"{fmt(m.get('prepare_worker_gap_ms'))} | "
            f"{fmt(m.get('kdn_fetch_ms'))} | "
            f"{fmt(m.get('kv_ack_ms'))} | "
            f"{fmt(m.get('kv_inject_queue_wait_ms'))} | "
            f"{fmt(m.get('kv_inject_exec_ms'))} | "
            f"{fmt(m.get('text_prefill_build_ms'))} | "
            f"{fmt(m.get('ready_queue_wait_ms'))} | "
            f"{fmt(m.get('forward_wait_ms'))} | "
            f"{fmt(m.get('knowledge_fetch_ms'))} | "
            f"{fmt(m.get('knowledge_preparation_total_ms'))} | "
            f"{fmt(m.get('vllm_compute_to_first_token_ms'))} | "
            f"{fmt(r.get('client_send_delay_ms'))} | "
            f"{fmt(r.get('wall_ms'))} | "
            f"{r.get('error')}"
        )

    print("\n" + "=" * 160)
    print("Average Performance Summary")
    print("=" * 160)

    metric_names = [
        # ("total_prefill_ms", "Average total prefill time"),
        ("proxy_before_vllm_ms", "Average time inside proxy before vLLM"),
        ("proxy_queue_wait_ms", "Average queue waiting time inside proxy"),
        ("knowledge_fetch_ms", "Average knowledge fetch time"),
        ("knowledge_preparation_total_ms", "Average total knowledge preparation time"),
        ("vllm_compute_to_first_token_ms", "Average actual vLLM compute time to first token"),
        ("prepare_queue_wait_ms", "Average prepare queue wait time"),
        ("prepare_exec_ms", "Average prepare execution time"),
        ("ready_queue_wait_ms", "Average ready queue wait time"),
        ("ready_dequeue_to_forward_ms", "Average wait after ready dequeue before forward"),
        ("proxy_wait_until_forward_ms", "Average total proxy wait until forward"),
        ("proxy_enqueue_to_forward_ms", "Average proxy enqueue to forward time"),
        ("proxy_recv_to_forward_ms", "Average proxy recv to forward time"),
        ("proxy_admission_ms", "Average proxy admission time"),
        ("route_select_ms", "Average route select time"),
        ("prepare_worker_gap_ms", "Average prepare worker gap time"),
        ("prepare_total_ms", "Average prepare total time"),
        ("kdn_fetch_ms", "Average KDN fetch time"),
        ("kv_inject_queue_wait_ms", "Average KV inject queue wait time"),
        ("kv_inject_exec_ms", "Average KV inject execution time"),
        ("text_prefill_build_ms", "Average text prefill build time"),
        ("forward_wait_ms", "Average forward wait time"),
        ("vllm_first_token_ms", "Average vLLM first token time"),
        ("instance_exec_to_first_token_ms", "Average instance execution to first token"),
        ("kv_ack_ms", "Average kv ack time"),
        ("actual_prepare_total_ms", "Average actual prepare total time"),
        ("prepare_buffer_wait_ms", "Average prepare buffer wait time"),
        ("actual_ready_queue_ms", "Average actual ready queue time"),
        ("actual_vllm_internal_ms", "Average actual vLLM internal time"),
        ("predict_queue_wait_ms", "Average predict queue wait time"),
        ("predict_error_ms", "Average predict error time"),
    ]

    for metric_key, metric_label in metric_names:
        vals: List[int] = []
        for r in results:
            v = r["metrics"].get(metric_key)
            if isinstance(v, int):
                vals.append(v)
        if vals:
            print(f"{metric_label}: {int(statistics.mean(vals))} ms")

    prefill_vals = collect_metric("total_prefill_ms")
    if prefill_vals:
        print(f"Prefill avg: {int(statistics.mean(prefill_vals))} ms")
        print(f"Prefill min: {min(prefill_vals)} ms")
        print(f"Prefill max: {max(prefill_vals)} ms")

    wall_vals = [r["wall_ms"] for r in results if isinstance(r.get("wall_ms"), int)]
    if wall_vals:
        print(f"Average end-to-end wall time: {int(statistics.mean(wall_vals))} ms")

    delay_vals = [r["client_send_delay_ms"] for r in results if isinstance(r.get("client_send_delay_ms"), int)]
    if delay_vals:
        print(f"Average client send delay: {int(statistics.mean(delay_vals))} ms")

    if print_trace:
        print("\n" + "=" * 160)
        print("Per Request Trace JSON")
        print("=" * 160)
        for r in results:
            print(json.dumps({
                "idx": r.get("req_index"),
                "name": r.get("name"),
                "injection": r.get("injection_type"),
                "trace": r.get("trace", {}),
                "metrics": r.get("metrics", {}),
            }, ensure_ascii=False, indent=2))

    print(f"Mode: {mode}")
    print(f"Hybrid Pattern: {hybrid_pattern}")
    if target_rps is not None:
        print(f"Target RPS: {target_rps}")
    print(f"Total elapsed time: {total_elapsed_s:.3f} s")
    if total_elapsed_s > 0:
        print(f"Actual throughput: {len(results) / total_elapsed_s:.3f} req/s")
    print(f"Peak inflight requests: {peak_inflight}")

    warning_results = [
        (r.get("req_index"), r.get("trace_warnings", []))
        for r in results
        if r.get("trace_warnings")
    ]
    if warning_results:
        print("\nTrace order warnings:")
        for req_idx, warnings in warning_results:
            for warning in warnings:
                print(f"  - idx={req_idx}: {warning}")

    total_requests = len(results)
    missing_meta = [r for r in results if r.get("meta_missing")]
    missing_first_token = [r for r in results if not r.get("trace_has_first_token")]
    missing_forward_start = [r for r in results if not r.get("trace_has_forward_start")]
    trace_key_counts = [int(r.get("trace_key_count", 0) or 0) for r in results]
    avg_trace_key_count = statistics.mean(trace_key_counts) if trace_key_counts else 0.0
    print("\nTrace Completeness Summary")
    print(f"Total requests: {total_requests}")
    print(f"Missing meta count: {len(missing_meta)}")
    print(f"Missing first_token count: {len(missing_first_token)}")
    print(f"Missing forward_start count: {len(missing_forward_start)}")
    print(f"Average trace key count: {avg_trace_key_count:.2f}")
    if missing_meta:
        print("Missing meta request details:")
        for r in missing_meta:
            print(
                f"  - idx={r.get('req_index')} name={r.get('name')} injection={r.get('injection_type')} "
                f"status={r.get('http_status')} wall_ms={r.get('wall_ms')}"
            )

    print("=" * 160)
    if gpu_monitor_enabled:
        summarize_gpu(gpu_samples or [], start_ts=exp_start_ts, end_ts=exp_end_ts)


def validate_args(args: argparse.Namespace) -> None:
    if args.requests <= 0:
        raise ValueError("--requests must be > 0")

    if args.mode not in ("concurrent", "rps"):
        raise ValueError("--mode must be one of: concurrent, rps")

    if args.mode == "concurrent":
        if args.concurrency is None or args.concurrency <= 0:
            raise ValueError("--concurrency must be > 0 in concurrent mode")
        if args.rps is not None:
            raise ValueError("--rps cannot be used in concurrent mode")

    if args.mode == "rps":
        if args.rps is None:
            raise ValueError("--rps is required in rps mode")
        if args.rps <= 0:
            raise ValueError("--rps must be > 0 in rps mode")

    parse_hybrid_pattern(args.hybrid_pattern)
    parse_gpu_ids(args.gpu_ids)
    if args.gpu_sample_interval <= 0:
        raise ValueError("--gpu-sample-interval must be > 0")


async def run_concurrent_mode(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    selected_templates: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    sem = asyncio.Semaphore(args.concurrency)
    inflight = 0
    peak_inflight = 0
    inflight_lock = asyncio.Lock()

    async def bounded_run(i: int, tpl: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal inflight, peak_inflight
        async with sem:
            async with inflight_lock:
                inflight += 1
                peak_inflight = max(peak_inflight, inflight)
            try:
                return await run_one(
                    client,
                    i,
                    args.base_url,
                    tpl,
                    args.hybrid_pattern,
                    scheduled_send_ts=time.time(),
                )
            finally:
                async with inflight_lock:
                    inflight -= 1

    tasks = [
        asyncio.create_task(bounded_run(i, tpl))
        for i, tpl in enumerate(selected_templates)
    ]
    results = await asyncio.gather(*tasks)
    return results, peak_inflight


async def run_rps_mode(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    selected_templates: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    inflight = 0
    peak_inflight = 0
    inflight_lock = asyncio.Lock()
    tasks: List[asyncio.Task] = []

    interval = 1.0 / float(args.rps)
    start_ts = time.time()

    async def fire_one(i: int, tpl: Dict[str, Any], scheduled_ts: float) -> Dict[str, Any]:
        nonlocal inflight, peak_inflight
        async with inflight_lock:
            inflight += 1
            peak_inflight = max(peak_inflight, inflight)
        try:
            return await run_one(
                client,
                i,
                args.base_url,
                tpl,
                args.hybrid_pattern,
                scheduled_send_ts=scheduled_ts,
            )
        finally:
            async with inflight_lock:
                inflight -= 1

    for i, tpl in enumerate(selected_templates):
        scheduled_ts = start_ts + i * interval
        delay = scheduled_ts - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        tasks.append(asyncio.create_task(fire_one(i, tpl, scheduled_ts)))

    results = await asyncio.gather(*tasks)
    return results, peak_inflight


async def main_async(args: argparse.Namespace) -> None:
    validate_args(args)
    gpu_ids = parse_gpu_ids(args.gpu_ids)

    workload = json.loads(Path(args.workload_file).read_text(encoding="utf-8"))
    req_templates = workload.get("requests", [])

    if not isinstance(req_templates, list) or not req_templates:
        raise ValueError("workload_file must contain a non-empty 'requests' list")

    if args.seed is not None:
        random.seed(args.seed)

    normalized_templates = [
        normalize_request_template(tpl, args) for tpl in req_templates
    ]

    selected_templates = build_selected_templates(
        req_templates=normalized_templates,
        total_requests=args.requests,
        allow_duplicate=args.allow_duplicate,
    )

    timeout = httpx.Timeout(300.0)
    gpu_samples: List[Dict[str, Any]] = []
    gpu_stop_event: Optional[asyncio.Event] = None
    gpu_task: Optional[asyncio.Task] = None
    if args.monitor_gpu:
        gpu_stop_event = asyncio.Event()
        gpu_task = asyncio.create_task(
            monitor_gpu(
                stop_event=gpu_stop_event,
                sample_interval_s=args.gpu_sample_interval,
                gpu_ids=gpu_ids,
            )
        )

    exp_start = time.time()
    async with httpx.AsyncClient(timeout=timeout) as client:
        if args.mode == "concurrent":
            results, peak_inflight = await run_concurrent_mode(client, args, selected_templates)
        else:
            results, peak_inflight = await run_rps_mode(client, args, selected_templates)
    exp_end = time.time()
    if gpu_task is not None and gpu_stop_event is not None:
        gpu_stop_event.set()
        gpu_samples = await gpu_task

    summarize(
        results=results,
        mode=args.mode,
        total_elapsed_s=(exp_end - exp_start),
        peak_inflight=peak_inflight,
        target_rps=args.rps if args.mode == "rps" else None,
        print_trace=args.print_trace,
        hybrid_pattern=args.hybrid_pattern,
        gpu_samples=gpu_samples,
        gpu_monitor_enabled=args.monitor_gpu,
        exp_start_ts=exp_start,
        exp_end_ts=exp_end,
    )


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--mode",
        choices=["concurrent", "rps"],
        required=True,
        help="run mode: concurrent or rps",
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="scheduler base url, e.g. http://127.0.0.1:7001",
    )
    parser.add_argument(
        "--workload-file",
        required=True,
        help="JSON file containing compact request templates",
    )

    parser.add_argument("--requests", type=int, required=True, help="total requests")
    parser.add_argument("--concurrency", type=int, default=None, help="concurrency limit in concurrent mode")
    parser.add_argument("--rps", type=float, default=None, help="request rate in rps mode")

    parser.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="allow selecting the same request template multiple times",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="random seed for reproducible sampling",
    )

    parser.add_argument(
        "--url-path",
        default="/v1/chat/completions",
        help="request url path",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="model name",
    )
    parser.add_argument(
        "--stream",
        type=str,
        default="true",
        help="whether to use stream mode: true/false",
    )
    parser.add_argument(
        "--rag",
        type=str,
        default="true",
        help="whether to enable RAG: true/false",
    )
    parser.add_argument(
        "--injection-type",
        choices=["text", "kvcache", "hybrid"],
        default="text",
        help="injection type",
    )
    parser.add_argument(
        "--hybrid-pattern",
        default="2:1",
        help="hybrid mode pattern as KVCache:text, e.g. 1:1, 2:1, 3:1",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1,
        help="max tokens",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.8,
        help="temperature",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=1.0,
        help="top_p",
    )
    parser.add_argument(
        "--knowledge-id",
        default=None,
        help="single knowledge id applied globally",
    )
    parser.add_argument(
        "--knowledge-ids",
        default=None,
        help="comma-separated knowledge ids applied globally",
    )
    parser.add_argument(
        "--print-trace",
        action="store_true",
        help="print per-request trace and metrics as JSON",
    )
    parser.add_argument(
        "--monitor-gpu",
        action="store_true",
        help="enable GPU utilization sampling via nvidia-smi during the experiment",
    )
    parser.add_argument(
        "--gpu-sample-interval",
        type=float,
        default=1.0,
        help="GPU sampling interval in seconds",
    )
    parser.add_argument(
        "--gpu-ids",
        default=None,
        help="comma-separated GPU ids to monitor, e.g. 0,1,2",
    )

    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
