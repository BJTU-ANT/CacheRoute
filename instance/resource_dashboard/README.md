# CacheRoute Instance Resource Dashboard

This dashboard is a lightweight validation frontend for the native Instance resource agent. It starts or connects to the Rust agent, fetches resource snapshots, and displays CPU, memory, GPU, network, admission-state, and raw JSON information.

It does **not** integrate with the Scheduler control plane yet. It also does not change Proxy forwarding, scheduling, KVCache injection, or existing Instance request behavior.

## Files

```text
instance/resource_dashboard/
├── README.md
├── dashboard_app.py          # local desktop monitor, recommended for interactive use
├── dashboard_server.py       # browser/server fallback for headless or remote use
└── static/
    ├── index.html
    ├── app.js
    └── style.css
```

## 1. Build/check the Rust agent

```bash
cargo check --manifest-path instance/resource_agent/Cargo.toml
```

## 2. Start the desktop dashboard

Run from the CacheRoute repository root:

```bash
python3 instance/resource_dashboard/dashboard_app.py \
  --agent-listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

A small local window should open and update the resource snapshot periodically. The dashboard starts the Rust resource agent automatically unless `--no-auto-start` is used.

By default, the dashboard auto-starts the Rust agent with:

```bash
cargo run --manifest-path instance/resource_agent/Cargo.toml -- \
  --listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

Use `--no-auto-start` if you want to start the agent yourself.

## 3. Headless or remote fallback

If the container has no graphical display, use the browser/server fallback:

```bash
python3 instance/resource_dashboard/dashboard_server.py \
  --dashboard-listen 0.0.0.0:9202 \
  --agent-listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

Open:

```text
http://127.0.0.1:9202
```

If the container does not use host networking, expose port `9202` from the container to the host.

## 4. Validate dashboard APIs

The browser/server fallback exposes:

```bash
curl -sS http://127.0.0.1:9202/api/health | python3 -m json.tool
curl -sS http://127.0.0.1:9202/api/snapshot | python3 -m json.tool
curl -sS http://127.0.0.1:9202/api/agent/status | python3 -m json.tool
```

It also supports:

```bash
curl -sS -X POST http://127.0.0.1:9202/api/agent/start | python3 -m json.tool
curl -sS -X POST http://127.0.0.1:9202/api/agent/stop | python3 -m json.tool
```

## 5. Validate direct Rust agent endpoint

```bash
curl -sS http://127.0.0.1:9201/healthz
curl -sS http://127.0.0.1:9201/v1/resource/snapshot | python3 -m json.tool
```

## Troubleshooting

### `cargo: command not found`

Use the CacheRoute Docker image built from `env/docker/Dockerfile`, or install Rust manually.

### Desktop window does not open

The desktop dashboard uses Python `tkinter`. In a container, you need a graphical display such as X11 forwarding, WSLg, or another desktop-capable environment.

For headless containers, use the browser/server fallback:

```bash
python3 instance/resource_dashboard/dashboard_server.py \
  --dashboard-listen 0.0.0.0:9202 \
  --agent-listen 127.0.0.1:9201
```

### Dashboard starts but snapshot is unavailable

Check whether the agent is reachable:

```bash
curl -sS http://127.0.0.1:9201/healthz
```

### GPU list is empty

Check whether the container can run:

```bash
nvidia-smi
```

Make sure the container was started with `--gpus all`.

### Port is already in use

Change ports with:

```bash
--agent-listen 127.0.0.1:<port>
--dashboard-listen 0.0.0.0:<port>
```

## Dashboard API

```text
GET  /api/health
GET  /api/snapshot
GET  /api/agent/status
POST /api/agent/start
POST /api/agent/stop
```

## Future Work

1. Connect dashboard snapshots to `scheduler/resource/control_plane.py`.
2. Add Instance-side queue and KVCache block metrics.
3. Replace `nvidia-smi` polling with NVML-based GPU collection.
4. Add WebSocket streaming if polling becomes insufficient.
5. Support multiple Instances in one dashboard.
