from __future__ import annotations

import unittest

from client.command_input import parse_curl_like_command


class ClientCommandInputTest(unittest.TestCase):
    def test_url_first_single_line_command(self) -> None:
        command = (
            'http://127.0.0.1:7001/v1/chat/completions '
            '-H "Content-Type: application/json" '
            "-d '{\"model\":\"llama3-70b\",\"messages\":[{\"role\":\"user\","
            "\"content\":\"What is vLLM?\"}],\"max_tokens\":64,\"stream\":true,"
            "\"RAG\":true,\"Injection_type\":\"kvcache\"}'"
        )

        parsed = parse_curl_like_command(command)

        self.assertEqual(parsed.url, "http://127.0.0.1:7001/v1/chat/completions")
        self.assertEqual(parsed.headers["Content-Type"], "application/json")
        self.assertEqual(parsed.body["model"], "llama3-70b")
        self.assertTrue(parsed.body["stream"])
        self.assertEqual(parsed.body["Injection_type"], "kvcache")

    def test_standard_multiline_curl_command(self) -> None:
        command = r'''curl http://127.0.0.1:7001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3-70b",
    "messages": [{"role": "user", "content": "What is vLLM?"}],
    "max_tokens": 64,
    "stream": false,
    "RAG": true
  }' '''

        parsed = parse_curl_like_command(command)

        self.assertEqual(parsed.body["messages"][0]["content"], "What is vLLM?")
        self.assertFalse(parsed.body["stream"])

    def test_curl_request_and_url_options(self) -> None:
        command = (
            "curl -X POST --url http://127.0.0.1:7001/v1/completions "
            "--data-raw '{\"model\":\"llama3-70b\",\"prompt\":\"hello\"}'"
        )

        parsed = parse_curl_like_command(command)

        self.assertEqual(parsed.url, "http://127.0.0.1:7001/v1/completions")
        self.assertEqual(parsed.body["prompt"], "hello")
        self.assertEqual(parsed.headers["Content-Type"], "application/json")

    def test_typographic_quotes_get_actionable_error(self) -> None:
        command = (
            "http://127.0.0.1:7001/v1/chat/completions "
            "-d ‘{\"model\":\"llama3-70b\",\"messages\":[]}’"
        )

        with self.assertRaisesRegex(ValueError, "typographic/full-width quote"):
            parse_curl_like_command(command)


if __name__ == "__main__":
    unittest.main()
