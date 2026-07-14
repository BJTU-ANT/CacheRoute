"""Batch utility for registering text blocks with KDN and optionally building KVCache dumps."""
import argparse
import json
import time
import requests
from pathlib import Path


def register_text_via_api(base_url: str, content: str, timeout: int = 60) -> dict:
    r = requests.post(
        f"{base_url.rstrip('/')}/knowledge/register_text",
        json={"content": content},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def build_kv_via_api(base_url: str, kid: str, args) -> dict:
    payload = {
        "kid": kid,
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
        "flushdb": args.flushdb,
    }

    r = requests.post(
        f"{base_url.rstrip('/')}/knowledge/build_kv",
        json=payload,
        timeout=args.kv_timeout,
    )
    r.raise_for_status()
    return r.json()


def delete_kid_via_api(base_url: str, kid: str, delete_kv: bool = True, timeout: int = 60) -> dict:
    r = requests.post(
        f"{base_url.rstrip('/')}/knowledge/delete",
        json={"knowledge_ids": [kid], "delete_kv": delete_kv},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def parse_count(count_value: str, total: int) -> int:
    if count_value == "all":
        return total
    n = int(count_value)
    if n < 0:
        raise ValueError("--count must be >= 0 or 'all'")
    return min(n, total)


def load_manifest(manifest_path: str):
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    base_dir = Path(manifest["base_dir"]).resolve()
    files = manifest["files"]

    if not isinstance(files, list):
        raise ValueError("manifest['files'] must be a list")

    for i, item in enumerate(files):
        if not isinstance(item, dict) or "file" not in item:
            raise ValueError(f"manifest['files'][{i}] must be {{'file': 'xxx.txt'}}")

    return base_dir, files


def main():
    parser = argparse.ArgumentParser(description="Batch register txt files and build KV locally with original logic.")
    parser.add_argument("--manifest", required=True, help="knowledge json path")
    parser.add_argument("--count", default="all", help="all or integer, e.g. 10")

    parser.add_argument("--api-url", required=True, help="vLLM OpenAI-compatible endpoint")
    parser.add_argument("--model", required=True, help="model name")

    parser.add_argument("--base-url", required=True, help="KDN server base url")
    parser.add_argument("--text-timeout", type=int, default=60)
    parser.add_argument("--kv-timeout", type=int, default=900)

    parser.add_argument("--max-tokens", type=int, default=1)
    parser.add_argument("--temperature", type=float, default=0.0)

    parser.add_argument("--redis-host", default="127.0.0.1")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--redis-db", type=int, default=0)
    parser.add_argument("--redis-password", default=None)

    parser.add_argument("--match", default="vllm@*")
    parser.add_argument("--scan-count", type=int, default=1000)
    parser.add_argument("--settle-wait-s", type=float, default=0.2)
    parser.add_argument("--settle-rounds", type=int, default=3)

    parser.add_argument("--flushdb", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.2)

    parser.add_argument("--result-json", default="batch_register_result.json")

    args = parser.parse_args()

    base_dir, all_items = load_manifest(args.manifest)
    selected_n = parse_count(args.count, len(all_items))
    items = all_items[:selected_n]



    print("===== START =====")
    print("manifest =", Path(args.manifest).resolve())
    print("base_dir =", base_dir)
    print("selected =", len(items))
    print("count =", args.count)
    print("=================")

    results = []
    missing_files = []

    for i, item in enumerate(items, start=1):
        file_path = (base_dir / item["file"]).resolve()

        if not file_path.exists():
            print(f"[{i}] ERROR: txt file not found -> {file_path}")

            missing_files.append(item["file"])

            results.append({
                "index": i,
                "file": item["file"],
                "ok": False,
                "error": "txt file not found",
            })
            continue

        print(f"[{i}] read {file_path.name}")

        try:
            content = file_path.read_text(encoding="utf-8")

            reg = register_text_via_api(args.base_url, content, timeout=args.text_timeout)
            kid = reg["kid"]
            status = reg["status"]
            length = reg["length"]

            print(f"[{i}] text ok: kid={kid} status={status} length={length}")

            recreated_from_existing = False

            if status == "exists":
                recreated_from_existing = True
                print(f"[{i}] existing kid={kid}, delete and overwrite")

                del_resp = delete_kid_via_api(
                    args.base_url,
                    kid,
                    delete_kv=True,
                    timeout=max(60, args.text_timeout),
                )
                print(f"[{i}] delete ok: {del_resp}")

                reg = register_text_via_api(args.base_url, content, timeout=args.text_timeout)
                kid = reg["kid"]
                status = reg["status"]
                length = reg["length"]

                print(f"[{i}] re-register ok: kid={kid} status={status} length={length}")

                if status != "created":
                    raise RuntimeError(f"re-register after delete still not created, got status={status}")

            out = build_kv_via_api(args.base_url, kid, args)

            print(
                f"[{i}] kv ok: kid={out['kid']} "
                f"dumped_keys={out.get('dumped_keys')} kv_dir={out.get('kv_dir')}"
            )

            results.append({
                "index": i,
                "file": item["file"],
                "kid": out["kid"],
                "register_status": status,
                "length": length,
                "dumped_keys": out.get("dumped_keys"),
                "kv_dir": out.get("kv_dir"),
                "ok": True,
                "skipped": False,
                "recreated_from_existing": recreated_from_existing,
            })

            print(
                f"[{i}] kv ok: kid={out['kid']} "
                f"dumped_keys={out.get('dumped_keys')} kv_dir={out.get('kv_dir')}"
            )

            results.append({
                "index": i,
                "file": item["file"],
                "kid": out["kid"],
                "register_status": status,
                "length": length,
                "dumped_keys": out.get("dumped_keys"),
                "kv_dir": out.get("kv_dir"),
                "ok": True,
            })

        except Exception as e:
            print(f"[{i}] error: {e}")
            results.append({
                "index": i,
                "file": item["file"],
                "ok": False,
                "error": str(e),
            })

        time.sleep(args.sleep)

    with open(args.result_json, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print("\n===== RESULT =====")
    for r in results:
        if r["ok"]:
            extra = " RECREATED" if r.get("recreated_from_existing") else ""
            print(
                f"[{r['index']}] {r['file']} "
                f"kid={r['kid']} dumped_keys={r['dumped_keys']}{extra}"
            )
        else:
            print(f"[{r['index']}] {r['file']} FAIL error={r['error']}")

    print(f"\nwrite result -> {Path(args.result_json).resolve()}")

    if missing_files:
        print("\n===== MISSING TXT FILES =====")
        for f in missing_files:
            print(f)
        print("total missing:", len(missing_files))

    success_cnt = sum(1 for r in results if r.get("ok") is True)
    fail_cnt = sum(1 for r in results if r.get("ok") is False)
    recreated_cnt = sum(1 for r in results if r.get("recreated_from_existing") is True)

    print("\n===== SUMMARY =====")
    print("selected items:", len(items))
    print("success:", success_cnt)
    print("failed:", fail_cnt)
    print("missing files:", len(missing_files))
    print("recreated existing:", recreated_cnt)


if __name__ == "__main__":
    main()