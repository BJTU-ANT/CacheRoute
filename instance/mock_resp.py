import time, json, asyncio
from typing import Dict, Any, AsyncGenerator

# ==================================================
#   Mock Engine：用于本地开发 / 未接 vLLM 时的输出模拟
# ==================================================
async def mock_chat_stream(payload: Dict[str, Any]) -> AsyncGenerator[bytes, None]:
    """模拟 chat/completions 的流式输出（OpenAI/vLLM 风格）"""
    base_id = f"chatcmpl-mock-{int(time.time())}"
    model_name = payload.get("model", "mock-model")
    created = int(time.time())

    # 1) 第一块：只带 role
    first_chunk = {
        "id": base_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant"},
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n".encode("utf-8")

    # 2) 中间内容块
    for piece in ["你好，", "这里是 Instance 模拟 ", "流式输出。"]:
        chunk = {
            "id": base_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": piece},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
        await asyncio.sleep(0.05)

    # 3) 最后一块：finish_reason=stop, delta 为空
    last_chunk = {
        "id": base_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }
    yield f"data: {json.dumps(last_chunk, ensure_ascii=False)}\n\n".encode("utf-8")

    # SSE 语义上的结束标记
    yield b"data: [DONE]\n\n"


async def mock_chat_completion(payload: Dict[str, Any]) -> Dict[str, Any]:
    """模拟 chat/completions 的非流式输出"""
    base_id = f"chatcmpl-mock-{int(time.time())}"
    model_name = payload.get("model", "mock-model")

    await asyncio.sleep(0.2)

    return {
        "id": base_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "你好，这里是 Instance 模拟完整回复。",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        },
    }


async def mock_text_completion(payload: Dict[str, Any]) -> Dict[str, Any]:
    """模拟 completions 模式"""
    base_id = f"cmpl-mock-{int(time.time())}"
    model_name = payload.get("model", "mock-model")
    prompt = payload.get("prompt", "")

    await asyncio.sleep(0.2)

    return {
        "id": base_id,
        "object": "text_completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "text": f"[mock completion for prompt={prompt!r}]",
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        },
    }