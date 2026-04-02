import asyncio
import aiohttp
from transformers import AutoTokenizer
from typing import Optional
from uuid import uuid4

def generate_prompt_with_tokens(tokenizer, target_token_count: int) -> str:
    """使用指定的 tokenizer 生成一个包含大致数量 token 的 prompt。"""
    if target_token_count <= 0:
        return ""

    # 为每次请求加入唯一前缀，降低长 prompt 场景下的前缀重用概率。
    uuid_prefix = f"request_uuid={uuid4()} "
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
    estimated_repeats = (remain_token_count // len(base_token_ids)) + 1
    prompt_ids = prefix_token_ids + (base_token_ids * estimated_repeats)[:remain_token_count]
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
