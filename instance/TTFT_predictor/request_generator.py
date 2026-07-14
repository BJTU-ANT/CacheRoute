"""Request-generation helpers used by TTFT prefill warmup and regression tests."""
import asyncio
import aiohttp
import time
from transformers import AutoTokenizer
from typing import Optional
import hashlib
import itertools
import time
import uuid

_REQUEST_SEQ = itertools.count()

def _timehash_uuid() -> str:
    """
    Generate a UUID string from a hash of the request timestamp.
    Append an incrementing sequence number to avoid collisions for concurrent calls within the same nanosecond.
    """
    raw = f"{time.time_ns()}-{next(_REQUEST_SEQ)}"
    digest32 = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return str(uuid.UUID(digest32))

def generate_prompt_with_tokens(tokenizer, target_token_count: int) -> str:
    """Generate a prompt whose token count is at least the target using the specified tokenizer."""
    if target_token_count <= 0:
        return ""

    # Add a unique prefix to each request to reduce prefix reuse probability for long prompts.
    uuid_prefix = f"request_uuid={_timehash_uuid()} "
    prefix_token_ids = tokenizer.encode(uuid_prefix, add_special_tokens=False)
    if not prefix_token_ids:
        return "Error"

    prompt_ids = list(prefix_token_ids)

    if len(prompt_ids) < target_token_count:
        base_text = "This is a long context test to measure the performance of the system. "
        base_token_ids = tokenizer.encode(base_text, add_special_tokens=False)
        if not base_token_ids:
            return "Error"

        remain_token_count = target_token_count - len(prompt_ids)
        body_token_ids = []
        inject_interval = 4  # Insert one random noise block every 4 base blocks to reduce long-prompt reuse probability.
        chunk_idx = 0
        while len(body_token_ids) < remain_token_count:
            body_token_ids.extend(base_token_ids)
            chunk_idx += 1

            if chunk_idx % inject_interval == 0:
                noise_text = f"noise={_timehash_uuid().replace('-', '')[:8]} "
                noise_token_ids = tokenizer.encode(noise_text, add_special_tokens=False)
                if noise_token_ids:
                    body_token_ids.extend(noise_token_ids)

        prompt_ids.extend(body_token_ids[:remain_token_count])
    else:
        prompt_ids = prompt_ids[:target_token_count]

    prompt = tokenizer.decode(prompt_ids, skip_special_tokens=False)

    # After decode and re-tokenization, token count may be slightly below target due to tokenizer boundary changes.
    # Pad a second time to ensure final token count >= target_token_count, allowing slight overshoot.
    final_token_ids = tokenizer.encode(prompt, add_special_tokens=False)
    if len(final_token_ids) >= target_token_count:
        return prompt

    padding_text = " padding_chunk_for_ttft_measurement."
    while len(final_token_ids) < target_token_count:
        prompt += padding_text
        final_token_ids = tokenizer.encode(prompt, add_special_tokens=False)
    return prompt

async def send_test_request(
    session: aiohttp.ClientSession, 
    host: str, 
    port: int, 
    model: str, 
    prompt: str
) -> Optional[float]:
    """
    Send one request to the vLLM server to trigger load.
    Return elapsed seconds from request start to the first streaming chunk.
    
    Returns:
        Optional[float]: TTFT in seconds; None on failure.
    """
    api_url = f"http://{host}:{port}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 1,
        "temperature": 0.01,
    }

    start_ts = time.perf_counter()
    try:
        async with session.post(api_url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                print(f"[WARN] send_test_request non-200: status={resp.status}, body={err_text[:200]}")
                return None
            
            # Wait for and read the first chunk to ensure the server has completed Prefill.
            async for chunk in resp.content.iter_any():
                if chunk:
                    # First chunk received; calculate TTFT.
                    return time.perf_counter() - start_ts
            
            # Some service implementations may not stream chunks; fall back to reading the body.
            fallback_text = await resp.text()
            if fallback_text.strip():
                return time.perf_counter() - start_ts
            return None
            
    except Exception as e:
        print(f"[WARN] send_test_request exception: {e}")
        return None
