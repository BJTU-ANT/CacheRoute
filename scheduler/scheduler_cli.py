"""Provides CLI commands for inspecting Scheduler resource and knowledge state."""
import argparse
import os
import shlex
import time
import requests

from urllib.parse import urlparse, urlunparse

from core import config

"""
Scheduler CLI (HTTP Client)

Why this CLI:
- Run Scheduler in one terminal
- Run this CLI in another terminal
- The CLI fetches realtime state via Scheduler debug endpoints

Required Scheduler endpoints:
- GET  /debug/status
- POST /debug/knowledge/peek
etc.

Usage:
  python3 scheduler_cli.py --base-url http://127.0.0.1:7001

Interactive commands:
  :help                         show help
  :knowledge_status             show scheduler knowledge summary (entries/dim/faiss/kdn source)
  :knowledge_schema             show KnowledgeUnit fields discovered from scheduler
  :knowledge_list [N]           list first N kids (default 10)
  :peek <kid> [kid2 ...]        show basic metadata for specified kids
  :refresh                      trigger refresh from KDN immediately
  :proxies [--all|-a]           show proxy pool information
  :strategy                     show scheduler routing strategy
  :exit / :quit                 exit CLI
"""

DEFAULT_BASE_URL = os.getenv("SCHEDULER_BASE_URL", config.SCHEDULER_BASE_URL).rstrip("/")
DEFAULT_TIMEOUT = 10


def http_get(base_url: str, path: str, timeout_s: int = DEFAULT_TIMEOUT) -> dict:
    r = requests.get(f"{base_url}{path}", timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def http_post(base_url: str, path: str, payload: dict, timeout_s: int = DEFAULT_TIMEOUT) -> dict:
    r = requests.post(f"{base_url}{path}", json=payload, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def http_get_url(url: str, timeout_s: int = DEFAULT_TIMEOUT) -> dict:
    r = requests.get(url, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "None"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))


def infer_cp_url(base_url: str) -> str:
    u = urlparse(base_url)
    host = u.hostname or "127.0.0.1"
    port = u.port

    # Conservative fallback: default to 7002 when no port is provided
    cp_port = 7002 if port is None else (7002 if port == 7001 else port + 1)

    scheme = u.scheme or "http"
    return urlunparse((scheme, f"{host}:{cp_port}", "", "", "", ""))


def cmd_status(base_url: str):
    s = http_get(base_url, "/debug/status")
    print("[STATUS]")
    print(f"  knowledge_loaded: {s.get('knowledge_loaded')}")
    print(f"  entries: {s.get('entries')}")
    print(f"  dim: {s.get('dim')}")
    print(f"  faiss_total: {s.get('faiss_total')}")
    # print(f"  kdn_base_url: {s.get('kdn_base_url')}")
    print(f"  last_refresh_ts: {s.get('last_refresh_ts')} ({_fmt_ts(s.get('last_refresh_ts'))})")

    print("  [KDN]")
    print(f"    alive: {s.get('kdn_alive', 0)}")
    print(f"    last_selected: {s.get('kdn_last_selected')} (id={s.get('kdn_last_selected_id')})")
    print(f"    last_refresh_ok: {s.get('kdn_last_refresh_ok')}  "
          f"ts={s.get('kdn_last_refresh_ts')} ({_fmt_ts(s.get('kdn_last_refresh_ts'))})")
    reason = s.get("kdn_last_refresh_reason", "") or ""
    if reason:
        print(f"    last_refresh_reason: {reason}")
    addrs = s.get("kdn_alive_addrs", []) or []
    if addrs:
        shown = addrs[:10]
        suffix = " ..." if len(addrs) > 10 else ""
        print(f"    alive_addrs[{len(addrs)}]: {shown}{suffix}")

    sample = s.get("sample_kids") or []
    if sample:
        print("    sample_kids[0:10]:")
        for k in sample:
            print(f"        - {k}")


def cmd_schema(base_url: str):
    s = http_get(base_url, "/debug/status")
    fields = s.get("unit_fields") or []
    print("[KNOWLEDGE_SCHEMA]")
    if not fields:
        print("  (empty)  -> maybe knowledge not loaded or no sample unit")
        return
    for f in fields:
        print(f"  - {f}")


def cmd_list(base_url: str, n: int = 10):
    s = http_get(base_url, "/debug/status")
    kids = s.get("sample_kids") or []
    kids = kids[: max(1, n)]
    print(f"[KNOWLEDGE_LIST] first {len(kids)} kids:")
    for k in kids:
        print(f"  - {k}")


def cmd_peek(base_url: str, kids: list[str]):
    kids = [k.strip().lower() for k in kids if k.strip()]
    if not kids:
        print("[ERROR] usage: :peek <kid> [kid2 ...]")
        return

    payload = {
        "kids": kids,
        # By default, show only safe fields to avoid printing large embeddings
        "need_fields": ["length", "avail_kdn_servers", "avail_llm_systems", "kv_ready","kv_dumped_keys"],
    }
    r = http_post(base_url, "/debug/knowledge/peek", payload)
    items = r.get("items") or []
    miss = r.get("miss") or []

    print("[PEEK]")
    for it in items:
        kid = it.get("kid")
        length = it.get("length")
        servers = it.get("avail_kdn_servers")
        llm_servers = it.get("avail_llm_systems")
        kv_ready = it.get("kv_ready")
        kv_dumped_keys = it.get("kv_dumped_keys")
        print(f"  kid: {kid}")
        print(f"    length: {length}")
        print(f"    avail_kdn_servers: {servers}")
        print(f"    avail_llm_systems: {llm_servers}")
        print(f"    kv_ready: {kv_ready}")
        print(f"    kv_dumped_keys: {kv_dumped_keys}")

    if miss:
        print("[MISS]")
        for k in miss:
            print(f"  - {k}")


def cmd_refresh(base_url: str):
    r = http_post(base_url, "/admin/refresh_knowledge", payload={})
    print("[REFRESH]")
    print(r)

def cmd_kdn(cp_url: str, include_dead: bool = False):
    data = http_get_url(f"{cp_url}/v1/kdn/list?include_dead={'true' if include_dead else 'false'}")
    kdns = data if isinstance(data, list) else (data.get("items") or [])

    print("[KDN_POOL]")
    print(f"  count: {len(kdns)}  include_dead={include_dead}")

    for k in kdns:
        kid = k.get("kdn_id")
        host = k.get("host")
        port = k.get("port")
        alive = k.get("is_alive")
        last_seen = k.get("last_seen_at")

        # Support both shapes: flattened fields or a load sub-object
        items = k.get("items")
        qps_1m = k.get("qps_1m")
        if items is None and isinstance(k.get("load"), dict):
            items = k["load"].get("items")
        if qps_1m is None and isinstance(k.get("load"), dict):
            qps_1m = k["load"].get("qps_1m")

        ls = "-"
        if isinstance(last_seen, (int, float)):
            ls = time.strftime("%H:%M:%S", time.localtime(int(last_seen)))

        print(f"  - {kid}  {host}:{port}  alive={alive}  last_seen={ls}  items={items}  qps_1m={qps_1m}")

def cmd_proxies(cp_url: str, include_dead: bool = False):
    data = http_get_url(f"{cp_url}/v1/proxy/list?include_dead={'true' if include_dead else 'false'}")
    proxies = data if isinstance(data, list) else (data.get("items") or [])

    print("[PROXY_POOL]")
    print(f"  count: {len(proxies)}  include_dead={include_dead}")

    for p in proxies:
        pid = p.get("proxy_id")
        host = p.get("host")
        port = p.get("port")
        alive = p.get("is_alive")
        last_seen = p.get("last_seen_at")

        # ---- dynamic (compatible with flattened fields or a load sub-object) ----
        inflight = p.get("inflight")
        qps_1m = p.get("qps_1m")
        gpu_util = p.get("gpu_util")
        if isinstance(p.get("load"), dict):
            load = p["load"]
            if inflight is None:
                inflight = load.get("inflight")
            if qps_1m is None:
                qps_1m = load.get("qps_1m")
            if gpu_util is None:
                gpu_util = load.get("gpu_util")

        # ---- static capability (reported at registration or computed by the scheduler) ----
        max_capacity = p.get("max_capacity")
        instance_count = p.get("instance_count")
        kv_mem_per_instance_gb = p.get("kv_mem_per_instance_gb")
        kv_cache_pool_gb = p.get("kv_cache_pool_gb")
        if isinstance(p.get("load"), dict):
            load = p["load"]
            if max_capacity is None:
                max_capacity = load.get("max_capacity")
            if instance_count is None:
                instance_count = load.get("instance_count")
            if kv_mem_per_instance_gb is None:
                kv_mem_per_instance_gb = load.get("kv_mem_per_instance_gb")
            if kv_cache_pool_gb is None:
                kv_cache_pool_gb = load.get("kv_cache_pool_gb")

        # policy（requested inside proxyinfo; read directly if backend exposes flat fields, otherwise fall back to meta）
        kv_policy = p.get("kv_cache_update_policy")
        if kv_policy is None and isinstance(p.get("meta"), dict):
            kv_policy = p["meta"].get("kv_cache_update_policy")

        ls = "-"
        if isinstance(last_seen, (int, float)):
            ls = time.strftime("%H:%M:%S", time.localtime(int(last_seen)))

        print(
            f"  - {pid}  {host}:{port}  alive={alive}  last_seen={ls}  "
            f"inflight={inflight}  qps_1m={qps_1m}  gpu_util={gpu_util}  "
            f"max_cap={max_capacity}  inst={instance_count}  "
            f"kv_per_inst_gb={kv_mem_per_instance_gb}  kv_pool_gb={kv_cache_pool_gb}  "
            f"kv_policy={kv_policy}"
        )


def cmd_strategy(base_url: str):
    r = http_get(base_url, "/debug/strategy")
    print("[STRATEGY]")
    print(f"  name: {r.get('strategy')}")
    print(f"  proxy_count: {r.get('proxy_count')}")

    sample = r.get("proxies_sample") or []
    if sample:
        print("  proxies_sample:")
        for p in sample:
            print(f"    - {p.get('proxy_id')} {p.get('host')}:{p.get('port')} alive={p.get('is_alive')}")



def print_help():
    print("=" * 80)
    print("Scheduler CLI (HTTP)")
    print("")
    print("Commands:")
    print("  :help                  show help menu")
    print("  :knowledge_status      show scheduler knowledge summary")
    print("  :knowledge_schema      show KnowledgeUnit shema stored in scheduler")
    print("  :knowledge_list [N]    list first N kids (default 10)")
    print("  :peek <kid> [kid2 ...] show basic metadata for specified kids")
    print("  :refresh               trigger refresh from KDN immediately")
    print("  :kdn [--all|-a]        show KDN pool information")
    print("  :proxies [--all|-a]    show proxy pool information")
    print("  :strategy              show scheduler routing strategy")
    print("  :exit / :quit          exit CLI")
    print("=" * 80)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Scheduler base url, e.g. http://127.0.0.1:7001")
    ap.add_argument("--cp-url", default=os.getenv("SCHEDULER_CP_URL", ""),
                    help="Control plane url, e.g. http://127.0.0.1:7002")
    args = ap.parse_args()
    base_url = args.base_url.rstrip("/")
    cp_url = (args.cp_url.rstrip("/") if args.cp_url else infer_cp_url(base_url)).rstrip("/")

    print_help()
    print(f"[CLI] scheduler={base_url}")
    print(f"[CLI] scheduler control_plane={cp_url}")

    while True:
        try:
            line = input("[sch] ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            print("\n^C")
            break

        if not line:
            continue

        if line in (":exit", ":quit"):
            break

        if line in (":help", "help"):
            print_help()
            continue

        # status
        if line == ":knowledge_status":
            try:
                cmd_status(base_url)
            except Exception as e:
                print(f"[ERROR] status failed: {e}")
            continue

        # fields
        if line in (":knowledge_schema", ":fields"):
            try:
                cmd_schema(base_url)
            except Exception as e:
                print(f"[ERROR] schema failed: {e}")
            continue

        # list
        if line.startswith(":knowledge_list"):
            tokens = shlex.split(line)
            n = 10
            if len(tokens) >= 2:
                try:
                    n = int(tokens[1])
                except Exception:
                    n = 10
            try:
                cmd_list(base_url, n=n)
            except Exception as e:
                print(f"[ERROR] list failed: {e}")
            continue

        # peek
        if line.startswith(":peek "):
            tokens = shlex.split(line)
            kids = tokens[1:]
            try:
                cmd_peek(base_url, kids)
            except Exception as e:
                print(f"[ERROR] peek failed: {e}")
            continue

        # refresh
        if line == ":refresh":
            try:
                cmd_refresh(base_url)
            except Exception as e:
                print(f"[ERROR] refresh failed: {e}")
            continue

        # KDN
        if line.lower().startswith(":kdn"):
            tokens = shlex.split(line)
            include_dead = ("--all" in tokens) or ("-a" in tokens)
            try:
                cmd_kdn(cp_url, include_dead=include_dead)
            except Exception as e:
                print(f"[ERROR] KDN catch failed: {e}")
            continue

        # proxies
        if line.startswith(":proxies"):
            tokens = shlex.split(line)
            include_dead = ("--all" in tokens) or ("-a" in tokens)
            try:
                cmd_proxies(cp_url, include_dead=include_dead)
            except Exception as e:
                print(f"[ERROR] proxies failed: {e}")
            continue

        # strategy
        if line == ":strategy":
            try:
                cmd_strategy(base_url)
            except Exception as e:
                print(f"[ERROR] strategy failed: {e}")
                print("        ensure scheduler exposes GET /debug/strategy")
            continue

        print(f"[ERROR] Unknown command: {line!r}")
        print("        use ':help' to see available commands")

    print("bye.")


if __name__ == "__main__":
    main()
