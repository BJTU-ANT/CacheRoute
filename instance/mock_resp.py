"""Mock OpenAI-compatible responses used when Instance runs without a real vLLM backend."""
import time, json, asyncio
from typing import Dict, Any, AsyncGenerator

# ==================================================
#   Mock Engine: simulates output for local development or when vLLM is not connected
# ==================================================
async def mock_chat_stream(payload: Dict[str, Any]) -> AsyncGenerator[bytes, None]:
    """Simulate streaming chat/completions output in OpenAI/vLLM style."""
    base_id = f"chatcmpl-mock-{int(time.time())}"
    model_name = payload.get("model", "mock-model")
    created = int(time.time())

    # 1) First chunk: role only.
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

    # 2) Intermediate content chunks.
    for piece in ["Hello, ", "this is simulated Instance ", "streaming output."]:
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

    # 3) Final chunk: finish_reason=stop with an empty delta.
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

    # SSE completion marker.
    yield b"data: [DONE]\n\n"


async def mock_chat_completion(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Simulate non-streaming chat/completions output."""
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
                    "content": "Hello, this is a complete simulated Instance response.",
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
    """Simulate completions mode."""
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