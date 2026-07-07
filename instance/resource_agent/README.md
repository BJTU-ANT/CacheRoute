# CacheRoute Instance Resource Agent

This is a small native agent that runs on the same host as an Instance and exposes local resource snapshots for validation and later scheduling integration.

It does **not** change Instance heartbeat, Proxy scheduling, or injection behavior yet.

## Build

```bash
cargo check --manifest-path instance/resource_agent/Cargo.toml
```

## Run locally

```bash
cargo run --manifest-path instance/resource_agent/Cargo.toml -- \
  --listen 127.0.0.1:9101 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

## Validate

```bash
curl -sS http://127.0.0.1:9101/healthz
curl -sS http://127.0.0.1:9101/v1/resource/snapshot | python3 -m json.tool
```

A valid snapshot includes `schema_version`, `timestamp_ms`, `devices.cpu`, `devices.memory`, `devices.network`, optional `devices.gpu`, and `capacity_hint.admission_state`.
