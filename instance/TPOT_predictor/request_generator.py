import asyncio
import hashlib
import itertools
import json
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

import aiohttp

_REQUEST_SEQ = itertools.count()


def _timehash_uuid() -> str:
    raw = f"{time.time_ns()}-{next(_REQUEST_SEQ)}"
    digest32 = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return str(uuid.UUID(digest32))


def generate_prompt_with_tokens(tokenizer, target_token_count: int) -> str:
    """生成 token 数不小于目标值的 prompt。"""
    if target_token_count <= 0:
        return ""

    uuid_prefix = f"request_uuid={_timehash_uuid()} "
    prefix_token_ids = tokenizer.encode(uuid_prefix, add_special_tokens=False)
    if not prefix_token_ids:
        return "Error"

    prompt_ids = list(prefix_token_ids)
    if len(prompt_ids) < target_token_count:
        base_text = "This is a long context test for TPOT benchmark. "
        base_token_ids = tokenizer.encode(base_text, add_special_tokens=False)
        if not base_token_ids:
            return "Error"

        remain = target_token_count - len(prompt_ids)
        body_token_ids = []
        while len(body_token_ids) < remain:
            body_token_ids.extend(base_token_ids)
        prompt_ids.extend(body_token_ids[:remain])
    else:
        prompt_ids = prompt_ids[:target_token_count]

    prompt = tokenizer.decode(prompt_ids, skip_special_tokens=False)

    final_token_ids = tokenizer.encode(prompt, add_special_tokens=False)
    if len(final_token_ids) >= target_token_count:
        return prompt

    while len(final_token_ids) < target_token_count:
        prompt += " padding_chunk_for_tpot_benchmark."
        final_token_ids = tokenizer.encode(prompt, add_special_tokens=False)
    return prompt


@dataclass
class TokenStep:
    token_index: int
    delta_seconds: float


@dataclass
class TaskTPOTResult:
    success: bool
    ttft_seconds: Optional[float]
    token_steps: List[TokenStep]
    error: Optional[str] = None


def _extract_delta_content(event: dict) -> str:
    choices = event.get("choices") or []
    if not choices:
        return ""

    delta = choices[0].get("delta") or {}
    if isinstance(delta.get("content"), str):
        return delta["content"]

    # 兼容部分服务的多模态/结构化 delta.content
    content = delta.get("content")
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        return "".join(texts)
    return ""


async def send_stream_request_for_tpot(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    model: str,
    prompt: str,
    max_tokens: int,
    tokenizer,
) -> TaskTPOTResult:
    """
    正确解析 chat completion SSE stream：
    - 只在检测到“新增文本 token”时推进 token_index
    - 若单次 event 带来多个 token，按“新增 token 数”展开多条记录
    """
    api_url = f"http://{host}:{port}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": 0.01,
    }

    start_ts = time.perf_counter()
    last_token_ts: Optional[float] = None
    token_steps: List[TokenStep] = []
    ttft_seconds: Optional[float] = None

    assistant_text = ""
    prev_token_count = 0
    sse_buffer = ""

    try:
        async with session.post(api_url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()
                return TaskTPOTResult(
                    success=False,
                    ttft_seconds=None,
                    token_steps=[],
                    error=f"non-200 status={resp.status}, body={err[:200]}",
                )

            async for chunk in resp.content.iter_any():
                if not chunk:
                    continue

                sse_buffer += chunk.decode("utf-8", errors="ignore")
                while "\n\n" in sse_buffer:
                    raw_event, sse_buffer = sse_buffer.split("\n\n", 1)
                    lines = [ln.strip() for ln in raw_event.splitlines() if ln.strip()]
                    if not lines:
                        continue

                    data_payloads = [ln[5:].strip() for ln in lines if ln.startswith("data:")]
                    if not data_payloads:
                        continue

                    for data_str in data_payloads:
                        if data_str == "[DONE]":
                            break

                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        delta_text = _extract_delta_content(event)
                        if not delta_text:
                            continue

                        now_ts = time.perf_counter()
                        assistant_text += delta_text
                        new_token_count = len(tokenizer.encode(assistant_text, add_special_tokens=False))
                        added_tokens = new_token_count - prev_token_count
                        if added_tokens <= 0:
                            continue

                        total_delta = (
                            now_ts - start_ts
                            if last_token_ts is None
                            else now_ts - last_token_ts
                        )
                        per_token_delta = max(total_delta / added_tokens, 1e-9)

                        for i in range(added_tokens):
                            token_idx = prev_token_count + i + 1
                            delta = per_token_delta
                            if ttft_seconds is None and token_idx == 1:
                                ttft_seconds = now_ts - start_ts
                                delta = ttft_seconds
                            token_steps.append(TokenStep(token_index=token_idx, delta_seconds=delta))

                            if token_idx >= max_tokens:
                                break

                        prev_token_count = min(new_token_count, max_tokens)
                        last_token_ts = now_ts

                        if prev_token_count >= max_tokens:
                            break

                    if prev_token_count >= max_tokens:
                        break

                if prev_token_count >= max_tokens:
                    break

            if not token_steps:
                fallback = await resp.text()
                return TaskTPOTResult(
                    success=False,
                    ttft_seconds=None,
                    token_steps=[],
                    error=f"no decoded token in stream, fallback={fallback[:200]}",
                )

            return TaskTPOTResult(
                success=True,
                ttft_seconds=ttft_seconds,
                token_steps=token_steps,
                error=None,
            )

    except Exception as exc:
        return TaskTPOTResult(
            success=False,
            ttft_seconds=None,
            token_steps=[],
            error=str(exc),
        )


async def bounded_gather(coros: List, concurrency: int):
    semaphore = asyncio.Semaphore(concurrency)

    async def _run(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*[_run(c) for c in coros])
