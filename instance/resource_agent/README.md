# CacheRoute Instance Resource Agent

`instance/resource_agent/` contains the native Rust Resource Agent used to collect local Instance host snapshots. It is the lowest-level resource observer in the Instance resource-monitoring path.

The agent collects CPU, memory, network, and GPU information from the local machine and exposes it over a tiny HTTP API. It does **not** make scheduling decisions by itself.

```text
Rust Resource Agent
  └──> /v1/resource/snapshot
        └── demo_instance.py report loop
              └── Proxy control plane /v1/instance/resource_snapshot
                    └── InstancePool.resource
```

## Current status

After the demo integration work, there are two supported usage modes.

| Mode | Owner | Recommended use |
|---|---|---|
| Demo-managed | `test/demo_instance.py` starts, checks, reports, and cleans up the agent | Default local demo path |
| Standalone | User starts the Rust agent and optionally runs `proxy_reporter.py` manually | Debugging or advanced experiments |

`demo_instance.py` is now the recommended entry for resource monitoring in local demos. It starts or reuses the Rust agent, waits for `/healthz`, starts reporting only after Instance registration succeeds, and kills only the agent process group it started during shutdown.

## Build

Run from the repository root:

```bash
cargo check --manifest-path instance/resource_agent/Cargo.toml
```

## Standalone Rust agent

The default Resource Agent demo port is `9201`. CacheRoute uses `9101` for the KDN Server, so avoid `9101` for this agent unless KDN is not running.

```bash
cargo run --manifest-path instance/resource_agent/Cargo.toml -- \
  --listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

Validate:

```bash
curl -sS http://127.0.0.1:9201/healthz
curl -sS http://127.0.0.1:9201/v1/resource/snapshot | python3 -m json.tool
```

A valid snapshot contains:

```text
schema_version
agent_version
instance_id
timestamp_ms
devices.cpu
devices.memory
devices.network
devices.gpu, optional
capacity_hint.memory_free_ratio
capacity_hint.admission_state
```

## Demo-managed path

Start Proxy first:

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

Resource monitoring is enabled by default for `demo_instance.py`. The demo will:

1. register the Instance to the Proxy control plane;
2. start or reuse the Rust agent;
3. wait for `GET /healthz`;
4. periodically fetch `/v1/resource/snapshot`;
5. report snapshots to Proxy `/v1/instance/resource_snapshot`;
6. stop the agent process group on shutdown, if this demo started it.

Disable it with:

```bash
python3 demo_instance.py --no-resource-monitor
```

Use a different agent port when multiple demos share the same machine:

```bash
python3 demo_instance.py \
  --port 9001 \
  --resource-agent-listen 127.0.0.1:19201 \
  --resource-agent-url http://127.0.0.1:19201
```

## Standalone reporter

The Python reporter is still available for debugging:

```bash
python3 instance/resource_agent/proxy_reporter.py \
  --agent-url http://127.0.0.1:9201 \
  --proxy-cp-url http://127.0.0.1:8002 \
  --instance-id hp_127.0.0.1:9001 \
  --once
```

Each report includes the snapshot plus report metadata:

```json
{
  "instance_id": "hp_127.0.0.1:9001",
  "snapshot": {"schema_version": "resource_snapshot_v1"},
  "metadata": {
    "reported_instance_id": "hp_127.0.0.1:9001",
    "report_monotonic_ms": 123456,
    "report_wall_time_ms": 1710000000000,
    "agent_snapshot_timestamp_ms": 1710000000000
  }
}
```

This lets the Proxy distinguish collection time, report time, and receive time.

## Inspect Proxy-side resource state

```bash
curl -sS "http://127.0.0.1:8002/debug/instance_resources" | python3 -m json.tool
curl -sS "http://127.0.0.1:8002/v1/instance/list?include_dead=true" | python3 -m json.tool
```

Important normalized fields:

```text
resource_ts_ms                  # agent collection timestamp
resource_reported_at            # Proxy receive time, seconds
resource_report_monotonic_ms    # reporter monotonic timestamp
resource_report_wall_time_ms    # reporter wall-clock timestamp
reported_instance_id            # ID used by the reporter
```

## E2E validation

A lightweight smoke script is provided:

```bash
python3 test/demo_resource_monitor_e2e.py
```

The script starts a demo Proxy and Instance, waits until Proxy observes resource reports, terminates the Instance, and checks that the demo-owned Resource Agent is cleaned up. If another agent is already running on `9201`, use a different port:

```bash
python3 test/demo_resource_monitor_e2e.py \
  --agent-listen 127.0.0.1:19201 \
  --agent-url http://127.0.0.1:19201
```

## Troubleshooting

### `cargo not found`

Use the CacheRoute development image or install Rust/Cargo. The demo logs the command that would have been executed.

### Old Cargo or lockfile error

Upgrade Rust stable:

```bash
rustup update stable
rustup default stable
```

Avoid mixing `sudo cargo` and user Cargo installations.

### Permission denied under `target/`

A previous container/root build may have left root-owned artifacts:

```bash
sudo chown -R $USER:$USER instance/resource_agent/target
```

or remove the build directory:

```bash
sudo rm -rf instance/resource_agent/target
```

### Port already in use

Check:

```bash
ss -ltnp | grep 9201 || true
```

Then either stop the old process or run the demo on another agent port.

## Future work

- Use normalized resource state in Proxy-side Instance selection.
- Add Instance runtime queue/KVCache metrics beyond host-level resources.
- Replace `nvidia-smi` polling with a lower-overhead GPU collector such as NVML.
- Support multiple local Instances with per-Instance resource dashboards.
