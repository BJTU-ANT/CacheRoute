"""
client.py

交互式 Client 工具，用于向 Scheduler 发送 OpenAI 风格的 HTTP 请求。

特性：
  - 启动后进入命令行 REPL，等待用户输入一行“类 curl 命令”：
      http://127.0.0.1:7001/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "...", ...}'
  - 支持解析：
      * URL
      * 多个 -H/--header 头部
      * -d/--data/--data-raw JSON 负载
  - 对请求做基本有效性校验（路径和 JSON payload 是否匹配 OpenAI chat/completions 或 completions）
  - 校验通过后发送 POST 请求，并打印响应（状态码 + headers + body）
  - 所有关键步骤都有日志输出，便于调试

注意：
  - 当前版本默认使用 POST 方法；
  - 仅支持 JSON 请求体，Content-Type 自动补全为 application/json。
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

# ----------------- 日志配置 -----------------
logger = logging.getLogger("client")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s - %(message)s",
    )

# ----------------- 数据结构 -----------------
@dataclass
class ParsedRequest:
    """解析后的请求结构"""
    url: str
    headers: Dict[str, str]
    body: Dict[str, Any]

# ----------------- 解析逻辑 -----------------

def parse_cli_line(line: str) -> ParsedRequest:
    """
    解析一行用户输入，例如：
      http://127.0.0.1:7001/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "..."}'

    支持：
      - URL 在第一个位置
      - -H/--header "Key: Value"
      - -d/--data/--data-raw 'JSON字符串'
    """
    line = line.strip()
    if not line:
        raise ValueError("输入为空")

    # 用 shlex 保留引号分组
    try:
        tokens = shlex.split(line)
    except ValueError as e:
        raise ValueError(f"命令行解析失败：{e}")

    if not tokens:
        raise ValueError("解析后为空，请检查输入")

    # 第一个 token 必须是 URL
    url = tokens[0]
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"第一个参数必须是完整 URL（含 http/https），当前：{url}")

    headers: Dict[str, str] = {}
    body_str: Optional[str] = None

    i = 1
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t in ("-H", "--header"):
            if i + 1 >= n:
                raise ValueError("缺少 -H/--header 后的值，例如 -H \"Content-Type: application/json\"")
            header_str = tokens[i + 1]
            # 简单按第一个 ":" 拆分
            if ":" not in header_str:
                raise ValueError(f"非法 Header 格式：{header_str}，应类似于 \"Key: Value\"")
            key, value = header_str.split(":", 1)
            headers[key.strip()] = value.strip()
            i += 2
        elif t in ("-d", "--data", "--data-raw"):
            if i + 1 >= n:
                raise ValueError("缺少 -d/--data/--data-raw 后的 JSON 字符串")
            body_str = tokens[i + 1]
            i += 2
        else:
            raise ValueError(f"无法识别的参数：{t}")

    # body 必须存在
    if body_str is None:
        raise ValueError("未提供请求体，请使用 -d/--data/--data-raw 传入 JSON 字符串")

    # 解析 JSON
    try:
        body = json.loads(body_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"请求体 JSON 解析失败：{e}")

    # 补齐 Content-Type
    ct = None
    for k in list(headers.keys()):
        if k.lower() == "content-type":
            ct = headers[k]
            break
    if ct is None:
        headers["Content-Type"] = "application/json"

    return ParsedRequest(url=url, headers=headers, body=body)

# ----------------- 校验逻辑 -----------------
def validate_openai_like_request(parsed: ParsedRequest) -> List[str]:
    """
    检查 URL 路径和 JSON body 是否符合 OpenAI 风格，并做字段白名单校验。

    规则：
      - URL 路径以 /v1/chat/completions 结尾：
          - 必须包含：model, messages
          - 允许额外字段：ALLOWED_OPTION_FIELDS["chat"]
      - URL 路径以 /v1/completions 结尾：
          - 必须包含：model, prompt
          - 允许额外字段：ALLOWED_OPTION_FIELDS["completions"]
    返回：
      如果 body 中出现未在“必要字段 + 允许字段”列表内的 key，视为非法字段，报错并拒绝发送请求。
    """
    errors: List[str] = []

    parsed_url = urlparse(parsed.url)
    path = parsed_url.path or ""
    body = parsed.body

    # 公共：model 必须存在
    model = body.get("model")
    if not isinstance(model, str) or not model:
        errors.append("body.model 缺失或不是非空字符串")

    # 判定模式
    mode: Optional[str]
    if path.endswith("/v1/chat/completions"):
        mode = "chat"
    elif path.endswith("/v1/completions"):
        mode = "completions"
    else:
        mode = None

    # 按模式做字段校验
    if mode is None:
        # 未识别的路径，给个提示，但不做强字段限制
        logger.warning("未识别的路径 %s，不进行严格字段白名单校验", path)
        return errors

    # 必要字段检查
    required = REQUIRED_FIELDS.get(mode, set())
    missing = [k for k in required if k not in body]
    if missing:
        errors.append(
            f"{mode} 请求缺少必要字段：{', '.join(missing)}"
        )

    # 额外字段白名单判断
    allowed_options = ALLOWED_OPTION_FIELDS.get(mode, set())
    allowed_all = required | allowed_options

    # body 中出现，但不在允许集合里的字段，都视为非法
    extra_keys = set(body.keys()) - allowed_all
    if extra_keys:
        errors.append(
            f"{mode} 请求包含未被允许的字段：{', '.join(sorted(extra_keys))}。"
            f" 当前仅允许：{', '.join(sorted(allowed_all))}"
        )
    return errors

# ----------------- 发送请求 -----------------

def send_request(parsed: ParsedRequest, timeout: float = 60.0) -> requests.Response:
    """
    使用 requests 向 Scheduler 发送 POST 请求。
    """
    logger.info("发送请求 → %s", parsed.url)
    logger.debug("请求头: %s", parsed.headers)
    logger.debug("请求体: %s", parsed.body)

    resp = requests.post(
        parsed.url,
        headers=parsed.headers,
        json=parsed.body,
        timeout=timeout,
        stream=True,
    )
    return resp

# ----------------- REPL 主循环 -----------------

def print_help() -> None:
    msg = r"""
用法示例（直接在 REPL 输入一行）：

  http://127.0.0.1:7001/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "xxx","messages":[{"role":"user","content":"Hello"}]}'

  http://127.0.0.1:7001/v1/completions -d '{"model": "xxx","prompt":"test"}'

命令：
  :help      显示本帮助
  :quit      退出
  :exit      退出
"""
    print(msg)

def _is_stream_requested(body: dict) -> bool:
    v = body.get("stream", False)
    # 兼容你现在传的 "True"/"False" 字符串
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y")
    return bool(v)

def _stream_and_print_sse(resp: requests.Response) -> None:
    """
    解析 OpenAI 风格 SSE：
      data: {...json...}
      data: [DONE]
    并将内容片段拼接为可读文本输出。
    """
    print("=" * 80)
    print(f"[RESPONSE] HTTP {resp.status_code} (streaming)")
    print("- Headers:")
    print(json.dumps(dict(resp.headers), ensure_ascii=False, indent=2))
    print("- Stream:")

    full_text_parts: List[str] = []

    # iter_lines 会按 \n 切分，SSE 的事件间隔是空行；我们只处理以 data: 开头的行
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue

        # 有些实现可能会带 "data: " 前缀
        if line.startswith("data:"):
            data = line[len("data:"):].strip()
        else:
            # 容错：如果没有 data: 前缀就跳过
            continue

        if data == "[DONE]":
            break

        try:
            obj = json.loads(data)
        except Exception:
            # 非 JSON，直接打印原始内容
            print(data, end="", flush=True)
            continue

        # OpenAI chat.completions streaming: choices[0].delta.content
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

        # 兼容：有些实现会用 choices[0].text（completions）
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
    print("[FULL TEXT]")
    print("".join(full_text_parts))
    print("=" * 80)


def pretty_print_response(resp: requests.Response, request_body: dict) -> None:
    """打印响应状态、头部和 body（优先按 JSON 格式化）"""
    if _is_stream_requested(request_body):
        _stream_and_print_sse(resp)
        return

    print("=" * 80)
    print(f"[RESPONSE] HTTP {resp.status_code}")
    print("- Headers:")
    print(json.dumps(dict(resp.headers), ensure_ascii=False, indent=2))

    print("- Body:")
    text = resp.text
    # 尝试按 JSON 格式打印
    try:
        obj = resp.json()
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    except ValueError:
        # 不是合法 JSON，就原样输出
        print(text)
    print("=" * 80)


def run_repl() -> None:
    """
    交互式命令行入口：
      - 循环读取用户输入
      - :quit / :exit 退出
      - :help 显示帮助
      - 其他内容按“类 curl 命令行”解析并发送
    """
    print("=== CacheRoute Client REPL ===")
    print("输入一行 HTTP 请求（类 curl 格式），或输入 :help 查看示例，:quit 退出。")

    while True:
        try:
            line = input("client> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n收到退出信号，结束。")
            break

        if not line:
            continue

        # 统一处理命令，支持 :help / help / :quit / quit
        cmd = line.strip()
        # 去掉前导冒号，方便统一判断
        if cmd.startswith(":"):
            cmd = cmd[1:]
        cmd_lower = cmd.lower()

        if cmd_lower in ("quit", "exit"):
            print("退出。")
            break

        if cmd_lower in ("help", "h", "?"):
            print_help()
            continue

        # 解析 + 校验 + 发送
        try:
            parsed = parse_cli_line(line)
        except ValueError as e:
            logger.error("请求解析失败：%s", e)
            print(f"[ERROR] {e}")
            continue

        errors = validate_openai_like_request(parsed)
        if errors:
            logger.error("请求校验失败：%s", "; ".join(errors))
            print("[ERROR] 请求字段校验失败：")
            for msg in errors:
                print("  -", msg)
            continue

        # 发送 HTTP 请求
        try:
            resp = send_request(parsed)
        except requests.RequestException as e:
            logger.error("HTTP 请求失败：%s", e)
            print(f"[ERROR] HTTP 请求异常：{e}")
            continue

        pretty_print_response(resp, parsed.body)

if __name__ == "__main__":
    run_repl()