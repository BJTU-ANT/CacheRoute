# proxy/queue/knowledge.py
"""
proxy维护的知识注入方法，由准备队列调用
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

from core import forward_request

logger = logging.getLogger("proxy.queue.knowledge")


def format_retrieved_context(items: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for idx, it in enumerate(items, start=1):
        content = (it.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"({idx}) {content}")
    return "\n".join(lines).strip()


async def fetch_knowledge_from_kdn(kdn_base_url: str, knowledge_ids: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    向 KDN 请求知识文本（非流式）。
    返回：(items, miss)
    """
    if not knowledge_ids:
        return [], []

    url = f"{kdn_base_url.rstrip('/')}/knowledge/search/text"
    body = {
        "knowledge_ids": knowledge_ids,
        "need_fields": ["content", "length", "rel_path"],
    }

    content_bytes = b""
    async for chunk in forward_request(url, data=body, use_chunked=False):
        if chunk:
            content_bytes += chunk

    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = content_bytes.decode("utf-8", errors="ignore")

    try:
        resp = json.loads(text) if text else {}
    except json.JSONDecodeError:
        raise RuntimeError(f"KDN response is not valid JSON: {text[:200]}")

    items = resp.get("items") or []
    miss = resp.get("miss") or []
    if not isinstance(items, list):
        items = []
    if not isinstance(miss, list):
        miss = []

    return items, miss


def inject_rag_into_instance_body(instance_body: Dict[str, Any], endpoint_type: str, retrieved_context: str) -> Dict[str, Any]:
    """
    将 retrieved_context 注入 instance_body（OpenAI 风格），返回新 dict。
    """
    if not retrieved_context:
        return instance_body

    new_body = dict(instance_body)

    if endpoint_type == "chat/completions":
        msgs = list(new_body.get("messages") or [])
        system_prompt = (
            f"You are a helpful assistant. Use the following retrieved context to answer the user. If the context is not relevant, ignore it. {retrieved_context}"
        )
        msgs.insert(0, {"role": "system", "content": system_prompt})
        new_body["messages"] = msgs
        return new_body

    prompt = (new_body.get("prompt") or "")
    rag_prefix = (
        "You are a helpful assistant.\n"
        "Use the following retrieved context to answer the user. If the context is not relevant, ignore it.\n"
        f"### Retrieved Context\n{retrieved_context}\n"
        "### User Prompt\n"
    )
    new_body["prompt"] = rag_prefix + str(prompt)
    return new_body