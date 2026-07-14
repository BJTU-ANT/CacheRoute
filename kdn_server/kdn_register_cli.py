"""Unified KDN registration and KV build CLI for text and KVCache workflows."""
import argparse
import os
import shlex
import requests
from core import config
"""
Unified KDN registration/build tool for Text + KV.
--base-url http://127.0.0.1:9101 specifies KDN_API

HTTP endpoints:
- POST /knowledge/register_text
- POST /knowledge/build_kv
- POST /knowledge/search/text
- POST /knowledge/delete

Interactive mode:
- Single line: paste and press Enter -> register_text
- :file <path> -> register_text (file content)
- :buildkv <kid> [--api-url ... --model ... --max-tokens ... --redis-host ...]
- :buildkv_file <path> [kv args...] -> register_text(file), then build_kv(kid)
- :status <kid> -> search knowledge status
- :delete <kid> [--kv] -> delete knowledge
- :purge [--no-kv]  -> delete all base (!!!)

Command-line mode:
- --file <path>                 register text only
- --build-kv-kid <kid>          trigger build_kv only
- --build-kv-file <path>        register text first, then build_kv; recommended
- --delete-kid <kid>            delete text only
- --delete-kv                   delete KV; must be used with a text kid
- --status-kid <kid>            query the status of the specified kid
"""

DEFAULT_BASE_URL = config.KDN_BASE_URL
DEFAULT_WARN_LEN = config.DEFAULT_WARN_LEN

# build_kv defaults, kept consistent with server defaults.
DEFAULT_API_URL = config.DEFAULT_API_URL
DEFAULT_MODEL = config.DEFAULT_MODEL_SHORTNAME
DEFAULT_MAX_TOKENS = config.DEFAULT_MAX_TOKENS
DEFAULT_TEMPERATURE = config.DEFAULT_TEMPERATURE
DEFAULT_REDIS_HOST = config.DEFAULT_REDIS_HOST
DEFAULT_REDIS_PORT = config.DEFAULT_REDIS_PORT
DEFAULT_REDIS_DB = config.DEFAULT_REDIS_DB
DEFAULT_MATCH = config.DEFAULT_MATCH
DEFAULT_SCAN_COUNT =config. DEFAULT_SCAN_COUNT


def query_pool_status(base_url: str, sample_limit: int = 10, timeout_s: int = 30) -> dict:
    r = requests.post(
        f"{base_url.rstrip('/')}/knowledge/pool_status",
        json={"sample_limit": sample_limit},
        timeout=timeout_s,
    )
    r.raise_for_status()
    return r.json()


def print_pool_status(resp: dict):
    print(
        "[POOL STATUS]\n"
        f"  kdn_id: {resp.get('kdn_id')}\n"
        f"  db_dir: {resp.get('db_dir')}\n"
        f"  kv_root: {resp.get('kv_root')}\n"
        f"  scheduler_enabled: {resp.get('scheduler_enabled')}\n"
        f"  scheduler_registered: {resp.get('scheduler_registered')}\n"
        f"  total_blocks: {resp.get('total_blocks')}\n"
        f"  embedding_ready_blocks: {resp.get('embedding_ready_blocks')}\n"
        f"  kv_ready_blocks: {resp.get('kv_ready_blocks')}\n"
        f"  text_only_blocks: {resp.get('text_only_blocks')}\n"
        f"  avg_length: {resp.get('avg_length')}\n"
        f"  avg_dumped_keys_on_ready: {resp.get('avg_dumped_keys_on_ready')}\n"
        f"  max_dumped_keys: {resp.get('max_dumped_keys')}"
    )

    sample_items = resp.get("sample_items") or []
    if sample_items:
        print("\n  sample_items:")
        for i, it in enumerate(sample_items, start=1):
            print(
                f"    [{i}] kid={it.get('kid')} "
                f"len={it.get('length')} "
                f"embed_dim={it.get('embed_dim')} "
                f"kv_ready={it.get('kv_ready')} "
                f"dumped_keys={it.get('kv_dumped_keys')} "
                f"rel_path={it.get('rel_path')}"
            )


def register_text(base_url: str, content: str, timeout_s: int = 30) -> dict:
    r = requests.post(
        f"{base_url.rstrip('/')}/knowledge/register_text",
        json={"content": content},
        timeout=timeout_s,
    )
    r.raise_for_status()
    return r.json()

def delete_kids(base_url: str, kids: list[str], delete_kv: bool, timeout_s: int = 60) -> dict:
    r = requests.post(
        f"{base_url.rstrip('/')}/knowledge/delete",
        json={"knowledge_ids": kids, "delete_kv": delete_kv},
        timeout=timeout_s,
    )
    r.raise_for_status()
    return r.json()

def purge_all(base_url: str, delete_kv: bool = True, timeout_s: int = 120) -> dict:
    r = requests.post(
        f"{base_url.rstrip('/')}/knowledge/purge_all",
        json={"delete_kv": delete_kv},
        timeout=timeout_s,
    )
    r.raise_for_status()
    return r.json()

def build_kv(base_url: str, kid: str, kv_args: dict, timeout_s: int = 600) -> dict:
    payload = {"kid": kid}
    payload.update(kv_args)
    r = requests.post(
        f"{base_url.rstrip('/')}/knowledge/build_kv",
        json=payload,
        timeout=timeout_s,
    )
    r.raise_for_status()
    return r.json()


def query_kid_status(base_url: str, kid: str, timeout_s: int = 15) -> dict:
    # Prefer need_fields to avoid large content/embedding payloads.
    payload = {
        "knowledge_ids": [kid],
        "need_fields": [
            "rel_path",
            "length",
            "embed_dim",
            "embedding_head",
            "kv_ready",
            "kv_rel_dir",
            "kv_dumped_keys",
            "kv_updated_at",
        ],
    }
    r = requests.post(
        f"{base_url.rstrip('/')}/knowledge/search/text",
        json=payload,
        timeout=timeout_s,
    )
    r.raise_for_status()
    return r.json()


def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def print_text_ok(resp: dict):
    print(
        f"[TEXT OK] kid={resp.get('kid')}  "
        f"status={resp.get('status')}  "
        f"length={resp.get('length')}"
    )


def print_kv_ok(resp: dict):
    print(
        f"[KV OK] kid={resp.get('kid')}  "
        f"dumped_keys={resp.get('dumped_keys')}  "
        f"kv_dir={resp.get('kv_dir')}"
    )


def print_status(resp: dict, kid: str):
    items = resp.get("items") or []
    miss = resp.get("miss") or []
    if miss and kid in miss:
        print(f"[STATUS] kid={kid}  NOT_FOUND")
        return
    if not items:
        print(f"[STATUS] kid={kid}  EMPTY_RESPONSE")
        return

    it = items[0]
    # Compatibility: some implementations may use different field names.
    rel_path = it.get("rel_path") or it.get("path")
    length = it.get("length")
    import math

    embed_dim = it.get("embed_dim")
    emb_head = it.get("embedding_head")
    has_embedding = isinstance(emb_head, list) and len(emb_head) > 0

    # Use only the head for a lightweight sanity check, not a full norm.
    emb_head_l2 = None
    if has_embedding:
        try:
            emb_head_l2 = math.sqrt(sum(float(x) * float(x) for x in emb_head))
        except Exception:
            emb_head_l2 = None

    kv_ready = it.get("kv_ready", 0)
    kv_rel_dir = it.get("kv_rel_dir")
    kv_dumped_keys = it.get("kv_dumped_keys")
    kv_updated_at = it.get("kv_updated_at")

    print(
        "[STATUS]\n"
        f"  kid: {kid}\n"
        f"  rel_path: ./text_database/{rel_path}\n"
        f"  length: {length}\n"
        f"  embedding: {'yes' if has_embedding else 'no'}"
        + (f" (dim={embed_dim})" if embed_dim is not None else "")
        + ("\n  embedding_head[0:10]: " + str(emb_head) if emb_head else "")
        + ("\n  embedding_head_l2: " + f"{emb_head_l2:.4f}" if emb_head_l2 is not None else "")
        + "\n"
          f"  kv_ready: {kv_ready}\n"
          f"  kv_rel_dir: ./KV_database/{kv_rel_dir}\n"
          f"  kv_dumped_keys: {kv_dumped_keys}\n"
          f"  kv_updated_at: {kv_updated_at}"
    )


def parse_kv_cli_tokens(tokens: list[str]) -> dict:
    """
    Parse optional build_kv arguments for both interactive commands and command-line mode.
    Supports:
      --api-url, --model, --max-tokens, --temperature,
      --redis-host, --redis-port, --redis-db, --redis-password,
      --match, --scan-count, --flushdb
    """
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--api-url", default=DEFAULT_API_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--redis-host", default=DEFAULT_REDIS_HOST)
    ap.add_argument("--redis-port", type=int, default=DEFAULT_REDIS_PORT)
    ap.add_argument("--redis-db", type=int, default=DEFAULT_REDIS_DB)
    ap.add_argument("--redis-password", default=None)
    ap.add_argument("--match", default=DEFAULT_MATCH)
    ap.add_argument("--scan-count", type=int, default=DEFAULT_SCAN_COUNT)
    ap.add_argument("--flushdb", action="store_true")


    ns = ap.parse_args(tokens)
    return {
        "api_url": ns.api_url,
        "model": ns.model,
        "max_tokens": ns.max_tokens,
        "temperature": ns.temperature,
        "redis_host": ns.redis_host,
        "redis_port": ns.redis_port,
        "redis_db": ns.redis_db,
        "redis_password": ns.redis_password,
        "match": ns.match,
        "scan_count": ns.scan_count,
        "flushdb": bool(ns.flushdb),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.getenv("KDN_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--timeout", type=int, default=30, help="timeout seconds for register_text")
    ap.add_argument("--warn-len", type=int, default=DEFAULT_WARN_LEN)

    # Non-interactive: text registration.
    ap.add_argument("--file", help="Register text from a file, then exit")

    # Non-interactive: KV build.
    ap.add_argument("--build-kv-kid", help="Build KV for an existing kid, then exit")
    ap.add_argument("--build-kv-file", help="Register text from file and then build KV, then exit")
    ap.add_argument("--status-kid", help="Query status for a kid, then exit")

    # Optional build_kv arguments for non-interactive mode.
    ap.add_argument("--api-url", default=DEFAULT_API_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--redis-host", default=DEFAULT_REDIS_HOST)
    ap.add_argument("--redis-port", type=int, default=DEFAULT_REDIS_PORT)
    ap.add_argument("--redis-db", type=int, default=DEFAULT_REDIS_DB)
    ap.add_argument("--redis-password", default=None)
    ap.add_argument("--match", default=DEFAULT_MATCH)
    ap.add_argument("--scan-count", type=int, default=DEFAULT_SCAN_COUNT)
    ap.add_argument("--flushdb", action="store_true")
    ap.add_argument("--delete-kid", help="Delete a kid (text, and optionally kv) then exit")
    ap.add_argument("--delete-kv", action="store_true", help="When deleting, also delete KV_database/<kid>/")

    ap.add_argument("--pool-status", action="store_true", help="Show overall KDN pool status, then exit")
    ap.add_argument("--sample-limit", type=int, default=10, help="Sample item count for --pool-status or :pool")

    args = ap.parse_args()
    base_url = args.base_url.rstrip("/")

    kv_args_from_flags = {
        "api_url": args.api_url,
        "model": args.model,
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
        "redis_host": args.redis_host,
        "redis_port": args.redis_port,
        "redis_db": args.redis_db,
        "redis_password": args.redis_password,
        "match": args.match,
        "scan_count": args.scan_count,
        "flushdb": bool(args.flushdb),
    }

    # ========== Non-interactive mode 1: register text only ==========
    if args.file:
        content = read_file(args.file)
        if not content.strip():
            print("[WARN] file content is empty (after stripping), skipped")
            raise SystemExit(0)
        resp = register_text(base_url, content, timeout_s=args.timeout)
        print_text_ok(resp)
        return

    # ========== Non-interactive mode 2: build KV only ==========
    if args.build_kv_kid:
        kid = args.build_kv_kid.strip().lower()
        resp = build_kv(base_url, kid, kv_args_from_flags, timeout_s=max(600, args.timeout))
        print_kv_ok(resp)
        return

    # ========== Non-interactive mode 3: file -> register -> build KV; most common ==========
    if args.build_kv_file:
        content = read_file(args.build_kv_file)
        if not content.strip():
            print("[WARN] file content is empty (after stripping), skipped")
            raise SystemExit(0)
        r1 = register_text(base_url, content, timeout_s=args.timeout)
        print_text_ok(r1)
        kid = str(r1.get("kid", "")).strip().lower()
        if not kid:
            raise SystemExit("[ERROR] register_text did not return kid")
        r2 = build_kv(base_url, kid, kv_args_from_flags, timeout_s=max(600, args.timeout))
        print_kv_ok(r2)
        return

    # ========== Non-interactive mode 4: query knowledge-block status <Kid> ==========
    if args.status_kid:
        kid = args.status_kid.strip().lower()
        resp = query_kid_status(base_url, kid, timeout_s=min(60, args.timeout))
        print_status(resp, kid)
        return

    if args.pool_status:
        resp = query_pool_status(base_url, sample_limit=args.sample_limit, timeout_s=min(60, args.timeout))
        print_pool_status(resp)
        return

    # ========== Non-interactive mode 5: delete knowledge blocks ==========
    if args.delete_kid:
        kid = args.delete_kid.strip().lower()
        resp = delete_kids(base_url, [kid], delete_kv=bool(args.delete_kv), timeout_s=max(60, args.timeout))
        print(resp)
        return

    # ========== Interactive mode ==========
    print("=" * 80)
    print("KDN Register CLI (Text + KV)")
    print(f"KDN: {base_url}")
    print("")
    print("Research commands:")
    print("  :status <kid>               show knowledge block status (text/embedding/kv)")
    print("  :pool [--sample-limit N]    show overall KDN pool status")
    print("")
    print("Text commands:")
    print("  <one line>                  register_text immediately")
    print('  :file <path>                register_text from file (recommended for long text)')
    print("")
    print("KV commands:")
    print("  :buildkv <kid> [kv args...]")
    print("  :buildkv_file <path> [kv args...]   (register file -> build kv)")
    print("")
    print("Delete commands:")
    print("  :delete <kid> [--kv]        delete knowledge (text; add --kv to also delete KV dir)")
    print("  :purge [--no-kv]            delete ALL knowledge (and KV by default)")
    print("")
    print("KV args (optional):")
    print("  --api-url ... --model ... --max-tokens N --temperature T")
    print("  --redis-host ... --redis-port N --redis-db N --redis-password ...")
    print("  --match ... --scan-count N --flushdb")
    print("")
    print("Other:")
    print("  :quit / :exit")
    print("=" * 80)

    while True:
        try:
            line = input("[kdn] ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            print("\n^C")
            break

        if not line:
            continue
        if line in (":quit", ":exit"):
            break

        # -------- KV: buildkv_file --------
        if line.startswith(":buildkv_file "):
            rest = line[len(":buildkv_file ") :].strip()
            # Use shlex to support quoted paths with spaces.
            tokens = shlex.split(rest)
            if not tokens:
                print("[ERROR] usage: :buildkv_file <path> [kv args...]")
                continue
            path = tokens[0]
            kv_tokens = tokens[1:]
            try:
                kv_args = parse_kv_cli_tokens(kv_tokens)
                content = read_file(path)
                if not content.strip():
                    print("[WARN] file content is empty (after stripping), skipped")
                    continue
                r1 = register_text(base_url, content, timeout_s=args.timeout)
                print_text_ok(r1)
                kid = str(r1.get("kid", "")).strip().lower()
                if not kid:
                    print("[ERROR] register_text did not return kid")
                    continue
                r2 = build_kv(base_url, kid, kv_args, timeout_s=max(600, args.timeout))
                print_kv_ok(r2)
            except Exception as e:
                print(f"[ERROR] buildkv_file failed: {e}")
            continue

        # -------- KV: buildkv --------
        if line.startswith(":buildkv "):
            rest = line[len(":buildkv ") :].strip()
            tokens = shlex.split(rest)
            if not tokens:
                print("[ERROR] usage: :buildkv <kid> [kv args...]")
                continue
            kid = tokens[0].strip().lower()
            kv_tokens = tokens[1:]
            try:
                kv_args = parse_kv_cli_tokens(kv_tokens)
                r = build_kv(base_url, kid, kv_args, timeout_s=max(600, args.timeout))
                print_kv_ok(r)
            except Exception as e:
                print(f"[ERROR] buildkv failed: {e}")
            continue

        # -------- Delete --------
        if line.startswith(":delete "):
            rest = line[len(":delete "):].strip()
            tokens = shlex.split(rest)
            if not tokens:
                print("[ERROR] usage: :delete <kid> [--kv]")
                continue
            kid = tokens[0].strip().lower()
            del_kv = ("--kv" in tokens[1:])
            try:
                resp = delete_kids(base_url, [kid], delete_kv=del_kv, timeout_s=max(60, args.timeout))
                print(resp)
            except Exception as e:
                print(f"[ERROR] delete failed: {e}")
            continue

        # -------- Delete_ALL --------
        if line.startswith(":purge"):
            tokens = shlex.split(line)
            del_kv = ("--no-kv" not in tokens)
            try:
                resp = purge_all(base_url, delete_kv=del_kv, timeout_s=max(120, args.timeout))
                print(resp)
            except Exception as e:
                print(f"[ERROR] purge failed: {e}")
            continue

        # -------- Status --------
        if line.startswith(":status ") or line.startswith(":stat "):
            rest = line.split(None, 1)[1].strip()
            kid = rest.lower()
            try:
                resp = query_kid_status(base_url, kid, timeout_s=min(60, args.timeout))
                print_status(resp, kid)
            except Exception as e:
                print(f"[ERROR] status failed: {e}")
            continue

        if line.startswith(":pool"):
            try:
                tokens = shlex.split(line)
                sample_limit = 10
                if "--sample-limit" in tokens:
                    idx = tokens.index("--sample-limit")
                    if idx + 1 >= len(tokens):
                        print("[ERROR] usage: :pool [--sample-limit N]")
                        continue
                    sample_limit = int(tokens[idx + 1])

                resp = query_pool_status(base_url, sample_limit=sample_limit, timeout_s=min(60, args.timeout))
                print_pool_status(resp)
            except Exception as e:
                print(f"[ERROR] pool status failed: {e}")
            continue

        # -------- Text: file --------
        if line.startswith(":file "):
            path = line[len(":file ") :].strip().strip('"').strip("'")
            try:
                content = read_file(path)
            except Exception as e:
                print(f"[ERROR] cannot read file: {e}")
                continue
            if not content.strip():
                print("[WARN] file content is empty (after stripping), skipped")
                continue
            try:
                resp = register_text(base_url, content, timeout_s=args.timeout)
                print_text_ok(resp)
            except Exception as e:
                print(f"[ERROR] register_text failed: {e}")
            continue


        # -------- Text: single line --------
        content = line
        if len(content) >= args.warn_len:
            print(
                f"[WARN] input length={len(content)} is very large. "
                "Some terminals may truncate long single-line paste. "
                "Use ':file <path>' or '--file <path>' for long text."
            )

        try:
            resp = register_text(base_url, content, timeout_s=args.timeout)
            print_text_ok(resp)
        except Exception as e:
            print(f"[ERROR] register_text failed: {e}")

    print("bye.")


if __name__ == "__main__":
    main()

