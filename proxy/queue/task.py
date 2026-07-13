# proxy/queue/task.py
from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List


@dataclass
class ProxyTask:
    """
    Internal proxy task wrapper.

    Notes:
    - Packages the information already parsed by the handler and passes it to
      the queue manager for forwarding to the selected instance.
    - This class can later be extended with prepare/ready states, injection
      latency, error codes, and related metadata.

    - chat: the ready worker places SSE bytes into response_queue, and the
      handler reads them through StreamingResponse.
    - completions: the ready worker also places bytes into the queue, and the
      handler concatenates them before parsing the JSON response.

    """
    request_id: Optional[int]
    req_obj: Any                            # Structured Request from scheduler to proxy (dataclass)
    instance_body: Dict[str, Any]           # Request body for downstream vLLM/instance (OpenAI style)

    instance_id: str                        # Selected instance information (InstancePool.InstanceInfo / Protocol InstanceLike)
    instance_host: str
    instance_port: int

    url_path: str                           # URL path for this request: "/v1/chat/completions" or "/v1/completions"

    kdn_addr: str | None = None

    # Per-task response channel: ready_worker pushes chunks and the handler pulls them.
    response_queue: "asyncio.Queue[Optional[bytes]]" = field(
        default_factory=lambda: asyncio.Queue(maxsize=128)
    )

    # Record the creation time.
    created_at: float = field(default_factory=lambda: time.time())

    # Task error, written when ready_worker or prepare_worker raises an exception.
    error: Optional[str] = None

    kv_ready_kids: List[str] = field(default_factory=list)
    text_only_kids: List[str] = field(default_factory=list)
    miss_kids: List[str] = field(default_factory=list)
    kv_ready_meta: list = field(default_factory=list)

    kv_ack: Dict[str, Any] = field(default_factory=dict)
    trace: Dict[str, int] = field(default_factory=dict)

    # Reservation state for the ready/prefill timeline.
    # Prediction stage: "prefill" (default) or "decode" (reserved for future modeling).
    predict_stage: str = "prefill"
    pred_slot_idx: int = -1
    pred_slot_ready_ts_ms: int = 0
    pred_forward_start_ts_ms: int = 0
    pred_prefill_start_ts_ms: int = 0
    pred_first_token_ts_ms: int = 0
    pred_decode_ms: int = 0
    pred_forward_end_ts_ms: int = 0
    pred_worker_free_ts_ms: int = 0
    pred_service_ms: int = 0
    has_started_forward: bool = False
    has_seen_first_token: bool = False
    reservation_seq: int = -1
    recompute_generation: int = 0

    def mark(self, key: str, ts_ms: int) -> None:
        self.trace[key] = int(ts_ms)
