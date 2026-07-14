# CacheRoute Proxy UI

`UI/proxy_ui/` contains the browser dashboard for observing the CacheRoute Proxy control plane. It is frontend / observability only: it does not change scheduling, forwarding, KDN, KVCache, Instance selection, or Resource Agent reporting behavior.

## Default URL

When launched by `test/demo_proxy.py`, the default browser URL is:

```text
http://127.0.0.1:8202
```

`demo_proxy.py` starts the Proxy UI by default for demo usability. If the UI fails to start, the demo prints a warning and continues starting the Proxy data plane and control plane.

Typical startup log:

```text
[demo_proxy] Proxy UI available at: http://127.0.0.1:8202
```

Failure example:

```text
[demo_proxy][WARN] Proxy UI failed to start: ...
```

## Run with demo Proxy

```bash
cd test
python3 demo_proxy.py \
  --host 127.0.0.1 \
  --port 8001 \
  --strategy round_robin \
  --injection-strategy iws
```

Useful flags:

| Flag | Description |
|---|---|
| `--proxy-ui` | Explicitly enable the browser Proxy UI. This is already the demo default. |
| `--no-proxy-ui` | Disable the UI subprocess. |
| `--proxy-ui-listen HOST:PORT` | UI server listen address, default `127.0.0.1:8202`. |
| `--proxy-ui-url URL` | Browser-facing URL printed in logs, useful for SSH tunnels or forwarded container ports. |

## Run standalone

Use this when Proxy is already running and you only want to start the UI server manually:

```bash
PROXY_UI_PROXY_CP_URL=http://127.0.0.1:8002 \
PROXY_UI_SCHEDULER_CP_URL=http://127.0.0.1:7002 \
python3 -m uvicorn UI.proxy_ui.proxy_ui_server:app --host 127.0.0.1 --port 8202
```

Open:

```text
http://127.0.0.1:8202
```

## What the dashboard shows

The polished Proxy UI exposes both current state and short in-browser trends.

### Summary and health

- Proxy health and `/debug/status` summary.
- Scheduler registration status, best effort and optional.
- Alive / stale / total Instance counts.
- Visible resource-report count.
- KDN topology-link count.
- Last refresh time and polling state.

### Controls

- `Refresh now`.
- `Pause polling` / `Resume polling`.
- Auto-refresh interval selector: 1s / 3s / 5s / 10s.
- Alive-only, stale-only, resources-only filters.
- Instance ID / host search.
- Sort selector for state, age, CPU, memory, GPU, and resource report time.
- Clear chart history.
- Copy diagnostics JSON.
- Theme toggle.

All controls are local browser display controls only.

### Charts

The UI keeps a short in-browser history buffer and draws lightweight Canvas charts for:

- CPU average utilization.
- Memory used ratio.
- GPU average utilization.
- Network RX / TX Mbps.
- Alive / stale Instance counts.

No backend persistence is required. Missing metrics degrade to empty chart segments.

### Instance and resource views

- Per-Instance cards with liveness badges, address, last-seen age, resource freshness, admission state, CPU/memory/GPU progress bars, and network mini-metrics.
- Detailed Instance table with sorting and local filtering.
- Resource snapshot table with freshness and admission-state fields.
- Topology, Scheduler, and Raw JSON tabs.
- Copy buttons for raw diagnostic payloads.

## API sources

The UI server proxies browser requests to existing Proxy / Scheduler APIs:

| UI endpoint | Source |
|---|---|
| `GET /api/config` | UI runtime configuration. |
| `GET /api/proxy/healthz` | Proxy control plane `/healthz`. |
| `GET /api/proxy/status` | Proxy control plane `/debug/status`. |
| `GET /api/proxy/instances?include_dead=true` | Proxy control plane `/v1/instance/list`. |
| `GET /api/proxy/resources?include_dead=true` | Proxy control plane `/debug/instance_resources`. |
| `GET /api/proxy/topology` | Proxy control plane `/v1/topology/kdn_links`. |
| `GET /api/scheduler/proxy` | Scheduler control plane `/v1/proxy/list`, best effort. |

Optional Scheduler/topology failures are displayed as unavailable or degraded states. They do not block the local Proxy and Instance panels from rendering.

## Related frontend URLs

| Component | Default frontend URL | Notes |
|---|---|---|
| Proxy UI | `http://127.0.0.1:8202` | This dashboard. Started by `demo_proxy.py` by default. |
| Instance Resource Dashboard | `http://127.0.0.1:9202` | Browser fallback for `instance/resource_dashboard/dashboard_server.py`. |
| Client UI | `http://127.0.0.1:7071/ui/client` | Started by `demo_client.py --with-ui`. |
| Scheduler UI | TBD | Planned. |
| KDN Server UI | TBD | Planned. |

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

Open the UI and confirm:

- summary cards update periodically;
- pause / resume works;
- refresh interval changes take effect;
- filters and search work;
- Instance cards and table show alive / stale state correctly;
- charts update as snapshots arrive;
- Scheduler unavailable state does not break the page;
- raw JSON can be expanded, collapsed, and copied;
- stopping `demo_proxy.py` cleans up the UI subprocess.
