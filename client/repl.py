"""Interactive CacheRoute client REPL with multiline curl support."""
from __future__ import annotations

from typing import Optional

import requests

from client import client as client_core
from client.command_input import command_needs_continuation, parse_curl_like_command


def print_help() -> None:
    print(
        r'''
Usage examples:

  URL-first one-line form:
    http://127.0.0.1:7001/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"llama3-70b","messages":[{"role":"user","content":"What is vLLM?"}],"max_tokens":64,"stream":true,"RAG":true,"Injection_type":"kvcache"}'

  Standard multiline curl form:
    curl http://127.0.0.1:7001/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d '{
        "model": "llama3-70b",
        "messages": [{"role": "user", "content": "What is vLLM?"}],
        "max_tokens": 64,
        "stream": true,
        "RAG": true,
        "Injection_type": "kvcache"
      }'

Commands:
  :help      Show this help message
  :quit      Exit
  :exit      Exit
  :cancel    Cancel the current multiline command
'''
    )


def _read_command() -> Optional[str]:
    """Read one complete command, continuing after ``\\`` or open quotes."""
    parts: list[str] = []
    prompt = "client> "

    while True:
        try:
            line = input(prompt)
        except EOFError:
            if not parts:
                return None
            print("\n[ERROR] Incomplete command discarded.")
            return ""
        except KeyboardInterrupt:
            if parts:
                print("\nMultiline command cancelled.")
                return ""
            print()
            return None

        if not parts and line.strip().lower() in {":quit", "quit", ":exit", "exit"}:
            return line.strip()

        if parts and line.strip().lower() in {":cancel", "cancel"}:
            print("Multiline command cancelled.")
            return ""

        parts.append(line)
        command = "\n".join(parts)
        if command_needs_continuation(command):
            prompt = "...> "
            continue
        return command


def run_repl() -> None:
    """Run the interactive CLI using the shared robust command parser."""
    print("=== CacheRoute Client REPL ===")
    print("Enter a URL-first or curl command; multiline input is supported.")
    print("Use :help for examples or :quit to exit.")

    while True:
        command = _read_command()
        if command is None:
            print("Exiting.")
            break
        if not command.strip():
            continue

        normalized_command = command.strip()
        command_name = normalized_command[1:] if normalized_command.startswith(":") else normalized_command
        command_name = command_name.lower()

        if command_name in {"quit", "exit"}:
            print("Exiting.")
            break
        if command_name in {"help", "h", "?"}:
            print_help()
            continue
        if command_name == "cancel":
            continue

        try:
            parsed = parse_curl_like_command(command)
        except ValueError as exc:
            client_core.logger.error("request parsing failed: %s", exc)
            print(f"[ERROR] {exc}")
            continue

        errors = client_core.validate_openai_like_request(parsed)
        if errors:
            client_core.logger.error("request validation failed: %s", "; ".join(errors))
            print("[ERROR] Request field validation failed:")
            for message in errors:
                print("  -", message)
            continue

        try:
            response = client_core.send_request(parsed)
        except requests.RequestException as exc:
            client_core.logger.error("HTTP request failed: %s", exc)
            print(f"[ERROR] HTTP request failed: {exc}")
            continue

        client_core.pretty_print_response(response, parsed.body)


if __name__ == "__main__":
    run_repl()
