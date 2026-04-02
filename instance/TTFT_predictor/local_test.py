import time
import asyncio
import aiohttp
from transformers import AutoTokenizer
from typing import Optional

def generate_prompt_with_tokens(tokenizer, target_token_count: int) -> str:
    """使用指定的 tokenizer 生成一个包含大致数量 token 的 prompt。"""
    if target_token_count <= 0: return ""
    base_text = "This is a long context test to measure the performance of the system. "
    base_token_ids = tokenizer.encode(base_text, add_special_tokens=False)
    if not base_token_ids: return "Error"
    estimated_repeats = (target_token_count // len(base_token_ids)) + 1
    prompt_ids = (base_token_ids * estimated_repeats)[:target_token_count]
    return tokenizer.decode(prompt_ids)

async def measure_ttft(
    session: aiohttp.ClientSession, 
    host: str, 
    port: int, 
    model: str, 
    prompt: str
) -> Optional[float]:
    """向 vLLM 服务器发送单个请求并测量 TTFT。"""
    api_url = f"http://{host}:{port}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 1,
        "temperature": 0.01,
    }

    start_time = time.perf_counter()
    try:
        async with session.post(api_url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                # 不打印错误，让调用者决定如何处理
                return None
            async for chunk in resp.content.iter_any():
                if chunk:
                    return time.perf_counter() - start_time
    except Exception:
        return None
    return None