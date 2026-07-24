"""Parse curl-like CacheRoute client input consistently for CLI and browser UI."""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse


_LINE_CONTINUATION_RE = re.compile(r"\\[ \t]*\r?\n")
_SMART_SHELL_QUOTE_RE = re.compile(
    r"(?:^|\s)(?:-H|--header|-d|--data|--data-raw)\s*([\u2018\u2019\u201c\u201d\uff02\uff07])"
)


@dataclass
class ParsedRequest:
    """Represent one parsed HTTP request."""

    url: str
    headers: Dict[str, str]
    body: Dict[str, Any]


def normalize_command_text(text: str) -> str:
    """Normalize pasted shell text without changing JSON payload content."""
    if not isinstance(text, str):
        raise ValueError("command input must be a string")

    # Browser/Windows paste may contain CRLF and non-breaking spaces.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")

    # A shell backslash followed by a newline is a line continuation, not an
    # argument. Python shlex otherwise emits the newline as a separate token.
    normalized = _LINE_CONTINUATION_RE.sub(" ", normalized)
    return normalized.strip()


def command_needs_continuation(text: str) -> bool:
    """Return True when an interactive command is visibly incomplete."""
    raw = text.rstrip()
    if not raw:
        return False
    if raw.endswith("\\"):
        return True

    try:
        shlex.split(normalize_command_text(text), posix=True)
    except ValueError as exc:
        message = str(exc).lower()
        return "no closing quotation" in message or "no escaped character" in message
    return False


def _raise_for_smart_shell_quotes(text: str) -> None:
    match = _SMART_SHELL_QUOTE_RE.search(text)
    if match is None:
        return
    raise ValueError(
        "typographic/full-width quote used as a shell argument delimiter; "
        "replace the outer quote after -H/-d with an ASCII single or double quote"
    )


def parse_curl_like_command(text: str) -> ParsedRequest:
    """Parse a URL-first or standard ``curl`` command.

    Accepted examples::

        http://127.0.0.1:7001/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"llama3-70b","messages":[]}'

        curl http://127.0.0.1:7001/v1/chat/completions \\
          -H "Content-Type: application/json" \\
          -d '{"model":"llama3-70b","messages":[]}'

    Only POST requests are supported. ``curl`` and ``-X POST`` are optional.
    """
    normalized = normalize_command_text(text)
    if not normalized:
        raise ValueError("input is empty")

    _raise_for_smart_shell_quotes(normalized)

    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError as exc:
        raise ValueError(
            f"failed to parse command line: {exc}; check that all shell quotes are ASCII and closed"
        ) from exc

    if not tokens:
        raise ValueError("no arguments were parsed; check the input")

    if tokens[0].lower() == "curl":
        tokens = tokens[1:]
    if not tokens:
        raise ValueError("curl command is missing a URL")

    url: Optional[str] = None
    headers: Dict[str, str] = {}
    body_str: Optional[str] = None

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token in ("-H", "--header"):
            if i + 1 >= len(tokens):
                raise ValueError(
                    'missing value after -H/--header, for example: -H "Content-Type: application/json"'
                )
            header_str = tokens[i + 1]
            if ":" not in header_str:
                raise ValueError(
                    f'invalid header format: {header_str}; expected "Key: Value"'
                )
            key, value = header_str.split(":", 1)
            headers[key.strip()] = value.strip()
            i += 2
            continue

        if token in ("-d", "--data", "--data-raw"):
            if i + 1 >= len(tokens):
                raise ValueError("missing JSON string after -d/--data/--data-raw")
            body_str = tokens[i + 1]
            i += 2
            continue

        if token in ("-X", "--request"):
            if i + 1 >= len(tokens):
                raise ValueError("missing HTTP method after -X/--request")
            method = tokens[i + 1].upper()
            if method != "POST":
                raise ValueError(f"only POST is supported; received method: {method}")
            i += 2
            continue

        if token == "--url":
            if i + 1 >= len(tokens):
                raise ValueError("missing URL after --url")
            if url is not None:
                raise ValueError("multiple URLs were provided")
            url = tokens[i + 1]
            i += 2
            continue

        parsed_token = urlparse(token)
        if parsed_token.scheme in ("http", "https") and parsed_token.netloc:
            if url is not None:
                raise ValueError("multiple URLs were provided")
            url = token
            i += 1
            continue

        raise ValueError(f"unrecognized argument: {token}")

    if url is None:
        raise ValueError("a complete HTTP/HTTPS URL is required")

    parsed_url = urlparse(url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        raise ValueError(f"invalid HTTP/HTTPS URL: {url}")

    if body_str is None:
        raise ValueError("request body is missing; pass JSON with -d/--data/--data-raw")

    try:
        body = json.loads(body_str)
    except json.JSONDecodeError as exc:
        raise ValueError(f"failed to parse request-body JSON: {exc}") from exc

    if not isinstance(body, dict):
        raise ValueError("request-body JSON must be an object")

    if not any(key.lower() == "content-type" for key in headers):
        headers["Content-Type"] = "application/json"

    return ParsedRequest(url=url, headers=headers, body=body)
