"""Provide an interactive REPL for sending OpenAI-compatible requests.

The client parses curl-like command lines, validates chat/completions or completions
payloads, sends JSON POST requests to the Scheduler, and prints normal or streaming
responses together with CacheRoute performance metadata.

Supported input:
  - A complete HTTP or HTTPS URL as the first argument.
  - Multiple ``-H``/``--header`` options.
  - A JSON body through ``-d``, ``--data``, or ``--data-raw``.

The client currently uses POST requests only and automatically adds
``Content-Type: application/json`` when the header is absent.
"""
from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from core.config import REQUIRED_FIELDS, ALLOWED_OPTION_FIELDS

import requests

# ----------------- Logging configuration -----------------
logger = logging.getLogger("client")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s - %(message)s",
    )

# ----------------- Data structures -----------------
@dataclass
class ParsedRequest:
    """Represent a parsed HTTP request."""
    url: str
    headers: Dict[str, str]
    body: Dict[str, Any]

# ----------------- Parsing -----------------

def parse_cli_line(line: str) -> ParsedRequest:
    """
    Parse one curl-like input line, for example:
      http://127.0.0.1:7001/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "..."}'

    Supported syntax:
      - URL in the first position.
      - ``-H``/``--header`` with ``"Key: Value"``.
      - ``-d``/``--data``/``--data-raw`` with a JSON string.
    """
    line = line.strip()
    if not line:
        raise ValueError("input is empty")

    # Use `shlex` to preserve quoted argument groups.
    try:
        tokens = shlex.split(line)
    except ValueError as e:
        raise ValueError(f"failed to parse command line: {e}")

    if not tokens:
        raise ValueError("no arguments were parsed; check the input")

    # The first token must be a complete URL.
    url = tokens[0]
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"the first argument must be a complete HTTP/HTTPS URL; received: {url}")

    headers: Dict[str, str] = {}
    body_str: Optional[str] = None

    i = 1
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t in ("-H", "--header"):
            if i + 1 >= n:
                raise ValueError("missing value after -H/--header, for example: -H \"Content-Type: application/json\"")
            header_str = tokens[i + 1]
            # Split only on the first colon.
            if ":" not in header_str:
                raise ValueError(f"invalid header format: {header_str}; expected \"Key: Value\"")
            key, value = header_str.split(":", 1)
            headers[key.strip()] = value.strip()
            i += 2
        elif t in ("-d", "--data", "--data-raw"):
            if i + 1 >= n:
                raise ValueError("missing JSON string after -d/--data/--data-raw")
            body_str = tokens[i + 1]
            i += 2
        else:
            raise ValueError(f"unrecognized argument: {t}")

    # A request body is required.
    if body_str is None:
        raise ValueError("request body is missing; pass a JSON string with -d/--data/--data-raw")

    # Parse the JSON body.
    try:
        body = json.loads(body_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"failed to parse request-body JSON: {e}")

    # Add Content-Type when it is absent.
    ct = None
    for k in list(headers.keys()):
        if k.lower() == "content-type":
            ct = headers[k]
            break
    if ct is None:
        headers["Content-Type"] = "application/json"

    return ParsedRequest(url=url, headers=headers, body=body)

# ----------------- Validation -----------------
def validate_openai_like_request(parsed: ParsedRequest) -> List[str]:
    """
    Validate the URL path and JSON body against the supported OpenAI-compatible schemas.

    Rules:
      - ``/v1/chat/completions`` requires ``model`` and ``messages`` and accepts
        fields listed in ``ALLOWED_OPTION_FIELDS["chat"]``.
      - ``/v1/completions`` requires ``model`` and ``prompt`` and accepts fields
        listed in ``ALLOWED_OPTION_FIELDS["completions"]``.

    Unknown body keys are reported as validation errors and prevent the request
    from being sent.
    """
    errors: List[str] = []

    parsed_url = urlparse(parsed.url)
    path = parsed_url.path or ""
    body = parsed.body

    # Both endpoint types require a non-empty model name.
    model = body.get("model")
    if not isinstance(model, str) or not model:
        errors.append("body.model is missing or is not a non-empty string")

    # Determine the endpoint mode.
    mode: Optional[str]
    if path.endswith("/v1/chat/completions"):
        mode = "chat"
    elif path.endswith("/v1/completions"):
        mode = "completions"
    else:
        mode = None

    # Validate fields for the selected endpoint mode.
    if mode is None:
        # Warn about unknown paths but do not enforce a strict field allowlist.
        logger.warning("unrecognized path %s; strict field allowlist validation is skipped", path)
        return errors

    # Check required fields.
    required = REQUIRED_FIELDS.get(mode, set())
    missing = [k for k in required if k not in body]
    if missing:
        errors.append(
            f"{mode} request is missing required fields: {', '.join(missing)}"
        )

    # Build the complete allowlist.
    allowed_options = ALLOWED_OPTION_FIELDS.get(mode, set())
    allowed_all = required | allowed_options

    # Treat body keys outside the allowlist as invalid.
    extra_keys = set(body.keys()) - allowed_all
    if extra_keys:
        errors.append(
            f"{mode} request contains unsupported fields: {', '.join(sorted(extra_keys))}."
            f" Allowed fields: {', '.join(sorted(allowed_all))}"
        )
    return errors

# ----------------- Request sending -----------------

def send_request(parsed: ParsedRequest, timeout: float = 60.0) -> requests.Response:
    """
    Send a POST request to the Scheduler with ``requests``.
    """
    logger.info("sending request -> %s", parsed.url)
    logger.debug("request headers: %s", parsed.headers)
    logger.debug("request body: %s", parsed.body)

    resp = requests.post(
        parsed.url,
        headers=parsed.headers,
        json=parsed.body,
        timeout=timeout,
        stream=True,
    )
    return resp

# ----------------- REPL -----------------

def print_help() -> None:
    msg = r"""
Usage examples (enter one line directly in the REPL):

    http://127.0.0.1:7001/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"xxx","messages":[{"role":"user","content":"Hello"}],"RAG":true,"stream":true,"Injection_type":"kvcache"}'

  http://127.0.0.1:7001/v1/completions -d '{"model": "xxx","prompt":"test","RAG":true,"Injection_type":"text"}'

Commands:
  :help      Show this help message
  :quit      Exit
  :exit      Exit
"""
    print(msg)

def _is_stream_requested(body: dict) -> bool:
    v = body.get("stream", False)
    # Accept string forms such as "True" and "False".
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y")
    return bool(v)


def _calc_metrics_from_trace(trace: Dict[str, int]) -> Dict[str, Optional[int]]:
    def duration(start_key: str, end_key: str) -> Optional[int]:
        if start_key in trace and end_key in trace:
            return int(trace[end_key]) - int(trace[start_key])
        return None

    prepare_queue_wait_ms = duration("proxy_enqueue_ms", "prepare_start_ms")
    prepare_exec_ms = duration("prepare_start_ms", "ready_enqueue_ms")
    ready_queue_wait_ms = duration("ready_enqueue_ms", "ready_dequeue_ms")
    instance_exec_to_first_token_ms = duration("forward_start_ms", "first_token_ms")

    total_prefill_ms = duration("proxy_enqueue_ms", "first_token_ms")
    proxy_before_vllm_ms = duration("proxy_enqueue_ms", "forward_start_ms")
    knowledge_fetch_ms = duration("kdn_fetch_start_ms", "kdn_fetch_end_ms")
    kv_ack_ms = duration("kv_ack_start_ms", "kv_ack_end_ms")

    proxy_queue_wait_ms = None
    if prepare_queue_wait_ms is not None or ready_queue_wait_ms is not None:
        proxy_queue_wait_ms = (prepare_queue_wait_ms or 0) + (ready_queue_wait_ms or 0)

    return {
        "total_prefill_ms": total_prefill_ms,
        "proxy_before_vllm_ms": proxy_before_vllm_ms,
        "proxy_queue_wait_ms": proxy_queue_wait_ms,
        "knowledge_fetch_ms": knowledge_fetch_ms,
        "knowledge_preparation_total_ms": prepare_exec_ms,
        "vllm_compute_to_first_token_ms": instance_exec_to_first_token_ms,
        "kv_ack_ms": kv_ack_ms,
    }


def _print_perf_summary_from_meta(meta: Dict[str, Any]) -> None:
    trace = meta.get("trace", {}) if isinstance(meta, dict) else {}
    metrics = _calc_metrics_from_trace(trace if isinstance(trace, dict) else {})

    def fmt(v: Any) -> str:
        return "N/A" if v is None else str(v)

    print("- Performance Summary:")
    # print(f"  Injection Type: {meta.get('injection_type', 'N/A')}")
    print(f"  Prefill Time (ms): {fmt(metrics.get('total_prefill_ms'))}")
    # print(f"  Time Inside Proxy Before vLLM (ms): {fmt(metrics.get('proxy_before_vllm_ms'))}")
    # print(f"  Waiting Time Inside Proxy Queue (ms): {fmt(metrics.get('proxy_queue_wait_ms'))}")
    # print(f"  Knowledge Fetch Time (ms): {fmt(metrics.get('knowledge_fetch_ms'))}")
    print(f"  Knowledge Preparation Time (ms): {fmt(metrics.get('knowledge_preparation_total_ms'))}")
    print(f"  vLLM Compute Time To First Token (ms): {fmt(metrics.get('vllm_compute_to_first_token_ms'))}")
    # print(f"  KV Injection Acknowledge Wait Time (ms): {fmt(metrics.get('kv_ack_ms'))}")
    # print(f"  Missing Knowledge IDs: {meta.get('miss_kids', [])}")


def _stream_and_print_sse(resp: requests.Response) -> None:
    """
    Parse an OpenAI-compatible SSE stream and print readable content fragments.

    The parser handles standard ``data`` events, the ``[DONE]`` sentinel, and the
    additional ``cacheroute_meta`` event emitted by CacheRoute.
    """
    print("=" * 80)
    print(f"[RESPONSE] HTTP {resp.status_code} (streaming)")
    print("- Headers:")
    print(json.dumps(dict(resp.headers), ensure_ascii=False, indent=2))
    print("- Stream:")

    full_text_parts: List[str] = []
    current_event = "message"
    cacheroute_meta: Dict[str, Any] = {}

    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            current_event = "message"
            continue

        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
            continue

        if not line.startswith("data:"):
            continue

        data = line[len("data:"):].strip()

        if data == "[DONE]":
            break

        if current_event == "cacheroute_meta":
            try:
                cacheroute_meta = json.loads(data)
            except Exception:
                cacheroute_meta = {"raw_meta": data}
            continue

        try:
            obj = json.loads(data)
        except Exception:
            print(data, end="", flush=True)
            continue

        delta = None
        choices = []

        try:
            choices = obj.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
        except Exception:
            delta = None

        piece = ""
        if isinstance(delta, dict):
            piece = delta.get("content") or ""

        if not piece:
            try:
                if choices:
                    piece = choices[0].get("text") or ""
            except Exception:
                piece = ""

        if piece:
            full_text_parts.append(piece)
            print(piece, end="", flush=True)

    print("\n" + "-" * 80)
    # print("[FULL TEXT]")
    # print("".join(full_text_parts))

    if cacheroute_meta:
        print("-" * 80)
        _print_perf_summary_from_meta(cacheroute_meta)

    print("=" * 80)


def pretty_print_response(resp: requests.Response, request_body: dict) -> None:
    """Print the response status, headers, and body, preferring formatted JSON."""
    if _is_stream_requested(request_body):
        _stream_and_print_sse(resp)
        return

    print("=" * 80)
    print(f"[RESPONSE] HTTP {resp.status_code}")
    print("- Headers:")
    print(json.dumps(dict(resp.headers), ensure_ascii=False, indent=2))

    print("- Body:")
    text = resp.text
    # Try to format the response as JSON.
    try:
        obj = resp.json()
        print(json.dumps(obj, ensure_ascii=False, indent=2))

        meta = obj.get("_cacheroute_meta")
        if isinstance(meta, dict):
            print("-" * 80)
            _print_perf_summary_from_meta(meta)

    except ValueError:
        # Print non-JSON responses unchanged.
        print(text)
    print("=" * 80)


def run_repl() -> None:
    """
    Run the interactive command-line loop.

    The REPL accepts ``:help``, ``:quit``, and ``:exit`` commands. Other input is
    parsed as a curl-like request, validated, and sent to the Scheduler.
    """
    print("=== CacheRoute Client REPL ===")
    print("Enter a curl-like HTTP request, use :help for examples, or :quit to exit.")

    while True:
        try:
            line = input("client> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExit signal received.")
            break

        if not line:
            continue

        # Normalize command aliases such as :help/help and :quit/quit.
        cmd = line.strip()
        # Remove a leading colon before command matching.
        if cmd.startswith(":"):
            cmd = cmd[1:]
        cmd_lower = cmd.lower()

        if cmd_lower in ("quit", "exit"):
            print("Exiting.")
            break

        if cmd_lower in ("help", "h", "?"):
            print_help()
            continue

        # Parse, validate, and send the request.
        try:
            parsed = parse_cli_line(line)
        except ValueError as e:
            logger.error("request parsing failed: %s", e)
            print(f"[ERROR] {e}")
            continue

        errors = validate_openai_like_request(parsed)
        if errors:
            logger.error("request validation failed: %s", "; ".join(errors))
            print("[ERROR] Request field validation failed:")
            for msg in errors:
                print("  -", msg)
            continue

        # Send the HTTP request.
        try:
            resp = send_request(parsed)
        except requests.RequestException as e:
            logger.error("HTTP request failed: %s", e)
            print(f"[ERROR] HTTP request failed: {e}")
            continue

        pretty_print_response(resp, parsed.body)

if __name__ == "__main__":
    run_repl()