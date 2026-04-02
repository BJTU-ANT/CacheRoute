import asyncio
import aiohttp
from transformers import AutoTokenizer
from typing import Optional
import hashlib
import itertools
import time
import uuid

_REQUEST_SEQ = itertools.count()

def _timehash_uuid() -> str:
    """
    基于请求时间戳哈希生成 UUID 字符串。
    额外拼接自增序号，避免同一纳秒内并发调用时碰撞。
    """
    raw = f"{time.time_ns()}-{next(_REQUEST_SEQ)}"
    digest32 = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return str(uuid.UUID(digest32))

def generate_prompt_with_tokens(tokenizer, target_token_count: int) -> str:
    """使用指定的 tokenizer 生成一个包含大致数量 token 的 prompt。"""
    if target_token_count <= 0:
        return ""

    # 为每次请求加入唯一前缀，降低长 prompt 场景下的前缀重用概率。
    uuid_prefix = f"request_uuid={_timehash_uuid()} "
    prefix_token_ids = tokenizer.encode(uuid_prefix, add_special_tokens=False)
    if not prefix_token_ids:
        return "Error"

    if len(prefix_token_ids) >= target_token_count:
        return tokenizer.decode(prefix_token_ids[:target_token_count])

    base_text = "This is a long context test to measure the performance of the system. "
    base_token_ids = tokenizer.encode(base_text, add_special_tokens=False)
    if not base_token_ids:
        return "Error"

    remain_token_count = target_token_count - len(prefix_token_ids)
    body_token_ids = []
    inject_interval = 4  # 每 4 个基础块插入一次随机噪声块，降低长 prompt 重用概率
    chunk_idx = 0
    while len(body_token_ids) < remain_token_count:
        body_token_ids.extend(base_token_ids)
        chunk_idx += 1

        if chunk_idx % inject_interval == 0:
            noise_text = f"noise={_timehash_uuid().replace('-', '')[:8]} "
            noise_token_ids = tokenizer.encode(noise_text, add_special_tokens=False)
            if noise_token_ids:
                body_token_ids.extend(noise_token_ids)

    prompt_ids = prefix_token_ids + body_token_ids[:remain_token_count]
    return tokenizer.decode(prompt_ids)

async def send_test_request(
    session: aiohttp.ClientSession, 
    host: str, 
    port: int, 
    model: str, 
    prompt: str
) -> bool:
    """
    向 vLLM 服务器发送单个请求以触发负载。
    不测量时间，仅确保收到首个 Token (代表 Prefill 完成)。
    
    Returns:
        bool: 请求是否成功发送并收到响应。
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

    try:
        async with session.post(api_url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                # 状态码非 200 视为失败
                return False
            
            # 等待并读取第一个 chunk，确保服务端已经完成了 Prefill
            async for chunk in resp.content.iter_any():
                if chunk:
                    # 收到数据即视为成功，无需继续读取
                    return True
            
            # 如果流结束了还没收到数据，视为失败
            return False
            
    except Exception:
        return False
