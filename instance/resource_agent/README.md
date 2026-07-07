# CacheRoute Instance Resource Agent

This is a small native Rust agent that runs on the same host as an Instance and exposes local resource snapshots for validation and later scheduling integration.

It does **not** change Instance heartbeat, Proxy scheduling, KDN injection, or Scheduler decisions yet.

## Build

```bash
cargo check --manifest-path instance/resource_agent/Cargo.toml
```

## Run locally

The default local port for the resource agent is `9201`. CacheRoute uses `9101` for the KDN Server in the full deployment path, so avoid using `9101` for the agent unless you know the KDN Server is not running.

```bash
cargo run --manifest-path instance/resource_agent/Cargo.toml -- \
  --listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

## Validate

```bash
curl -sS http://127.0.0.1:9201/healthz
curl -sS http://127.0.0.1:9201/v1/resource/snapshot | python3 -m json.tool
```

A valid snapshot includes `schema_version`, `timestamp_ms`, `devices.cpu`, `devices.memory`, `devices.network`, optional `devices.gpu`, and `capacity_hint.admission_state`.

## Dashboard

For local monitoring, use the Instance Resource Dashboard under:

```text
instance/resource_dashboard/
```

The dashboard can start this agent automatically and display CPU, memory, GPU, network, admission-state, and raw snapshot information.
