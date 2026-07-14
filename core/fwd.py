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
    Forward an HTTP POST request to an upstream service such as vLLM or Proxy.

    Args:
        url: Full upstream URL, for example
            "http://127.0.0.1:8000/v1/chat/completions".
        data: JSON payload sent upstream. This is usually an OpenAI-style
            request body or a serialized Request payload.
        use_chunked: When True, yield response bytes in 1024-byte chunks for
            streaming LLM output. When False, read the whole response and yield
            it once.
        extra_headers: Optional HTTP headers to pass through to the upstream
            service, such as Authorization or X-Request-ID.

    Behavior:
        - Uses aiohttp.ClientSession for the POST request.
        - Adds Authorization from OPENAI_API_KEY by default.
        - Treats 2xx and 4xx responses as upstream responses to pass through.
        - Logs and raises HTTPException for other HTTP statuses.
        - Maps aiohttp.ClientError to 502 Bad Gateway.
        - Maps unexpected errors to 500 Internal Server Error.
    """
    # Build ClientSession with the global AIOHTTP_TIMEOUT.
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Assemble upstream request headers.
        headers = {
            # Include OPENAI_API_KEY by default for protected vLLM/OpenAI-compatible APIs.
            "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}"
        }
        # Merge caller-provided headers, such as selected fields from the original request.
        if extra_headers:
            headers.update(extra_headers)   # extra_headers overrides default headers with the same names.

        try:
            # Send the JSON POST request to the upstream service with custom headers.
            async with session.post(url=url, json=data,
                                    headers=headers) as response:
                # Treat upstream 2xx and 4xx statuses as pass-through responses.
                if 200 <= response.status < 300 or 400 <= response.status < 500:  # noqa: E501
                    if use_chunked:
                        # Streaming mode: read up to 1024 bytes at a time and yield immediately.
                        async for chunk_bytes in response.content.iter_chunked(  # noqa: E501
                                1024):
                            yield chunk_bytes
                    else:
                        # Non-streaming mode: read the complete response body once.
                        content = await response.read()
                        yield content
                else:
                    # Other statuses are errors: log details and raise HTTPException.
                    error_content = await response.text()
                    try:
                        # Prefer JSON parsing so logs stay structured when possible.
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
            # Map aiohttp client-level errors, such as network failures, to 502.
            logging.error("ClientError occurred: %s", str(e))
            raise HTTPException(
                status_code=502,
                detail="Bad Gateway: Error communicating with upstream server.",
            ) from e
        except Exception as e:
            # Log unexpected errors and map them to 500.
            logging.error("Unexpected error: %s", str(e))
            raise HTTPException(status_code=500, detail=str(e)) from e
