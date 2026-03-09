# proxy/queue/task.py
from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List


@dataclass
class ProxyTask:
    """
    Proxy 内部任务封装。

    说明：
    - 把handler中已经解析好的信息打包起来，
      交给队列管理器去“按选择的 instance”转发。
    - 后续会在这里扩展：prepare/ready 状态、注入耗时、错误码等。

    - chat：ready worker 把 SSE bytes 放进 response_queue，handler 用 StreamingResponse 读取
    - completions：ready worker 同样放 bytes，handler 拼接后解析 JSON

    """
    request_id: Optional[int]
    req_obj: Any                            # scheduler -> proxy 的结构化 Request（dataclass）
    instance_body: Dict[str, Any]           # 下游 vLLM / instance 的请求体（OpenAI 风格）

    instance_id: str                        # 已选中的 instance 信息（InstancePool.InstanceInfo / Protocol InstanceLike）
    instance_host: str
    instance_port: int

    url_path: str                           # 本次请求对应的 URL path："/v1/chat/completions" or "/v1/completions"

    kdn_addr: str | None = None

    # per-task 响应通道：ready_worker push chunk，handler pull chunk
    response_queue: "asyncio.Queue[Optional[bytes]]" = field(
        default_factory=lambda: asyncio.Queue(maxsize=128)
    )

    # 记录创建时间
    created_at: float = field(default_factory=lambda: time.time())

    # 任务错误（ready_worker/prepare_worker 发生异常时写入）
    error: Optional[str] = None

    kv_ready_kids: List[str] = field(default_factory=list)
    text_only_kids: List[str] = field(default_factory=list)
    miss_kids: List[str] = field(default_factory=list)
    kv_ready_meta: list = field(default_factory=list)

    kv_ack: Dict[str, Any] = field(default_factory=dict)
    trace: Dict[str, int] = field(default_factory=dict)

    def mark(self, key: str, ts_ms: int) -> None:
        self.trace[key] = int(ts_ms)
