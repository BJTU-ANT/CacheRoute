# CacheRoute Proxy UI

Lightweight browser dashboard for observing the Proxy control plane without changing scheduling, forwarding, KDN, or KVCache behavior.

## Default behavior in `demo_proxy.py`

`test/demo_proxy.py` starts the browser Proxy UI by default for demo usability. The UI is observability-only: if it fails to start, `demo_proxy.py` prints a warning and continues starting the Proxy data/control planes.

Useful flags:

- `--no-proxy-ui`: disable the UI subprocess.
- `--proxy-ui-listen HOST:PORT`: choose where the UI server listens, default `127.0.0.1:8202`.
- `--proxy-ui-url URL`: choose the browser-facing URL printed in logs, useful when tunneling or forwarding ports.

When the UI starts correctly, the demo prints:

```text
[demo_proxy] Proxy UI available at: http://127.0.0.1:8202
```

If the UI subprocess exits early, cannot import, or the port is occupied, the demo prints a warning like:

```text
[demo_proxy][WARN] Proxy UI failed to start: ...
```

## Run with demo proxy

```bash
cd test
python3 demo_proxy.py \
  --host 127.0.0.1 \
  --port 8001 \
  --strategy round_robin \
  --injection-strategy iws
```

Disable the UI when needed:

```bash
cd test
python3 demo_proxy.py --no-proxy-ui
```

## Run standalone

```bash
PROXY_UI_PROXY_CP_URL=http://127.0.0.1:8002 \
PROXY_UI_SCHEDULER_CP_URL=http://127.0.0.1:7002 \
python3 -m uvicorn UI.proxy_ui.proxy_ui_server:app --host 127.0.0.1 --port 8202
```

## What the first version shows

- Proxy health and debug status.
- Instance pool records from `/v1/instance/list?include_dead=true`, including accurate `is_alive` state when the Proxy API provides it.
- Instance resource snapshots from `/debug/instance_resources`.
- KDN topology links from `/v1/topology/kdn_links`.
- Best-effort Scheduler registration state when `PROXY_UI_PROXY_ID` is configured.

The UI polls the Proxy control plane through the UI server, so the browser does not need direct CORS access to Proxy APIs. Optional Scheduler/topology failures are shown as unavailable states and do not block the local Proxy and Instance panels from rendering.

## Polished dashboard features

Issue #100 upgrades the first Proxy UI into a richer frontend-only dashboard while keeping the same observability-only contract.

- Sticky top navigation with Proxy/Scheduler badges, last refresh time, refresh, pause/resume, and theme controls.
- Auto-refresh interval selector for 1s / 3s / 5s / 10s polling.
- Local filters for alive-only, stale-only, resources-only, and instance-id/host search.
- Short in-browser chart history for CPU, memory, GPU utilization, network RX/TX, and alive/stale counts.
- Per-instance cards with liveness badges, resource freshness badges, progress bars, network mini-metrics, and admission state.
- Sortable Instance table by state, age, CPU, memory, GPU, or resource report time.
- Topology, Scheduler, and Raw JSON tabs so raw payloads remain available without dominating the main dashboard.
- Copy buttons for diagnostics JSON and individual raw payloads.
- Optional Scheduler/topology failures render as unavailable/degraded states and do not block local Proxy/Instance panels.

All controls are browser-local display controls only. They do not mutate Scheduler routing, Proxy instance selection, Proxy injection strategy, KDN behavior, KVCache behavior, Instance forwarding, or Resource Agent reporting semantics.

## Validation steps

Compile check:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile \
  UI/proxy_ui/proxy_ui_server.py \
  proxy/resource/p_control_plane.py \
  test/demo_proxy.py
```

Start Proxy:

```bash
cd test
python3 demo_proxy.py \
  --host 127.0.0.1 \
  --port 8001 \
  --strategy round_robin \
  --injection-strategy iws
```

Expected log when UI starts correctly:

```text
[demo_proxy] Proxy UI available at: http://127.0.0.1:8202
```

Start Instance:

```bash
cd test
python3 demo_instance.py \
  --host 127.0.0.1 \
  --port 9001 \
  --proxy-cp-url http://127.0.0.1:8002
```

Check Proxy APIs:

```bash
curl -sS http://127.0.0.1:8002/healthz | python3 -m json.tool
curl -sS http://127.0.0.1:8002/debug/status | python3 -m json.tool
curl -sS "http://127.0.0.1:8002/v1/instance/list?include_dead=true" | python3 -m json.tool
curl -sS "http://127.0.0.1:8002/debug/instance_resources" | python3 -m json.tool
```

Open the UI URL and confirm:

- Proxy status is visible.
- Instance pool rows are visible.
- Resource snapshot rows update periodically.
- Alive/stale state is accurate.
- Scheduler unavailable state does not break the page.
- Stopping `demo_proxy.py` cleans up the UI subprocess.
- No scheduling, routing, injection, KDN, KVCache, or Instance forwarding behavior changed.
