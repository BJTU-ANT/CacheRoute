# CacheRoute Instance Resource Dashboard

This dashboard is a lightweight validation frontend for the native Instance resource agent. It starts or connects to the Rust agent, fetches resource snapshots, and displays CPU, memory, GPU, network, admission-state, and raw JSON information in a browser.

It does **not** integrate with the Scheduler control plane yet. It also does not change Proxy forwarding, scheduling, KVCache injection, or existing Instance request behavior.

## Files

```text
instance/resource_dashboard/
├── README.md
├── dashboard_server.py
└── static/
    ├── index.html
    ├── app.js
    └── style.css
```

## 1. Build/check the Rust agent

```bash
cargo check --manifest-path instance/resource_agent/Cargo.toml
```

## 2. Start the dashboard

Run from the CacheRoute repository root:

```bash
python3 instance/resource_dashboard/dashboard_server.py \
  --dashboard-listen 0.0.0.0:9102 \
  --agent-listen 127.0.0.1:9101 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

By default, the dashboard auto-starts the Rust agent with:

```bash
cargo run --manifest-path instance/resource_agent/Cargo.toml -- \
  --listen 127.0.0.1:9101 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

Use `--no-auto-start` if you want to start the agent yourself.

## 3. Open the frontend

```text
http://127.0.0.1:9102
```

If the container does not use host networking, expose port `9102` from the container to the host.

## 4. Validate dashboard APIs

```bash
curl -sS http://127.0.0.1:9102/api/health | python3 -m json.tool
curl -sS http://127.0.0.1:9102/api/snapshot | python3 -m json.tool
curl -sS http://127.0.0.1:9102/api/agent/status | python3 -m json.tool
```

The dashboard also supports:

```bash
curl -sS -X POST http://127.0.0.1:9102/api/agent/start | python3 -m json.tool
curl -sS -X POST http://127.0.0.1:9102/api/agent/stop | python3 -m json.tool
```

## 5. Validate direct Rust agent endpoint

```bash
curl -sS http://127.0.0.1:9101/healthz
curl -sS http://127.0.0.1:9101/v1/resource/snapshot | python3 -m json.tool
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
