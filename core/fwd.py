import json
import logging
import os
from typing import Optional

import aiohttp

from fastapi import HTTPException
from jupyter_client.session import extract_header

from core.config import AIOHTTP_TIMEOUT


async def forward_request(url, data, use_chunked=True, extra_headers: Optional[dict] = None):
    """
        将请求以 HTTP POST 方式转发到下游（例如 vLLM 实例或 Proxy），并以异步流的形式返回响应内容。

        参数：
            url : str
                下游服务的完整 URL，例如 "http://127.0.0.1:8000/v1/chat/completions"。
            data : dict
                发送给下游的 JSON 数据，一般是 OpenAI 风格的请求体，或你封装好的 Request payload。
            use_chunked : bool
                是否按流式（chunked）方式返回：
                    - True ：按 1024 字节一块迭代返回（适合 LLM 流式输出）。
                    - False ：一次性读完响应并返回（只 yield 一次）。
            extra_headers : dict | None
                需要额外透传给下游的 HTTP 头，例如用户的 Authorization、X-Request-ID 等。

        行为说明：
            - 使用 aiohttp.ClientSession 发 POST 请求。
            - 默认会在 header 中加上 "Authorization: Bearer ${OPENAI_API_KEY}"，
              用于访问需要 OpenAI API Key 的下游。
            - 如果状态码是 2xx 或 4xx：
                * use_chunked=True  时，按流迭代返回响应体的字节块（async generator）。
                * use_chunked=False 时，一次性读完响应体并返回（仍然通过 yield 返回）。
            - 其它状态码视为错误，解析错误内容后打日志并抛出 HTTPException。
            - aiohttp.ClientError 会转换成 502 Bad Gateway。
            - 其它异常统一转成 500 Internal Server Error。
    """
    # 构造 ClientSession，设置总超时时间为 AIOHTTP_TIMEOUT
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 组装下游请求头
        headers = {
            # 默认带上环境变量中的 OPENAI_API_KEY，用于访问受保护的 vLLM / OpenAI 接口
            "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}"
        }
        # 如果上层提供了额外的 headers（例如用户原始请求头中的部分字段），则合并
        if extra_headers:
            headers.update(extra_headers)   # extra_headers 中的键会覆盖默认 headers 中的同名键

        try:
            # 向下游发送 POST 请求，使用 JSON 体和自定义 headers
            async with session.post(url=url, json=data,
                                    headers=headers) as response:
                #  下游返回 2xx 或 4xx 时，视为“正常响应”（4xx 多为参数错误、鉴权失败等）
                if 200 <= response.status < 300 or 400 <= response.status < 500:  # noqa: E501
                    if use_chunked:
                        # 流式模式：每次读取最多 1024 字节，边收到边向上游 yield
                        async for chunk_bytes in response.content.iter_chunked(  # noqa: E501
                                1024):
                            yield chunk_bytes
                    else:
                        # 非流式模式：一次性读取完整响应体（适合非流式 LLM 或管理接口）
                        content = await response.read()
                        yield content
                else:
                    # 其它状态码：记录错误日志并抛出 HTTPException
                    error_content = await response.text()
                    try:
                        # 优先尝试按 JSON 解析，便于结构化日志
                        error_content = json.loads(error_content)
                    except json.JSONDecodeError:
                        error_content = error_content

                    logging.error("Request failed with status %s: %s", response.status, error_content)
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Request failed with status {response.status}: "
                        f"{error_content}",
                    )

        except aiohttp.ClientError as e:
            # aiohttp 客户端级错误（网络问题、连接失败等），统一映射为 502
            logging.error("ClientError occurred: %s", str(e))
            raise HTTPException(
                status_code=502,
                detail="Bad Gateway: Error communicating with upstream server.",
            ) from e
        except Exception as e:
            # 其它未预期异常，记录后抛出 500
            logging.error("Unexpected error: %s", str(e))
            raise HTTPException(status_code=500, detail=str(e)) from e
