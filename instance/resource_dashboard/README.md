# CacheRoute Instance Resource Dashboard

`instance/resource_dashboard/` contains local visual frontends for the Rust Instance Resource Agent.

The dashboard is for **local observation and debugging**. It can start or connect to the Resource Agent and display CPU, memory, GPU, network, admission state, and raw snapshot data. The dashboard is separate from the demo resource-reporting path now owned by `test/demo_instance.py`.

```text
Local visual path:
  dashboard_app.py / dashboard_server.py
      └── Rust Resource Agent /v1/resource/snapshot

Demo reporting path:
  test/demo_instance.py
      └── Rust Resource Agent /v1/resource/snapshot
            └── Proxy /v1/instance/resource_snapshot
```

The dashboard does **not** make scheduling decisions and does not change Proxy forwarding, KVCache injection, or Scheduler behavior.

## Files

```text
instance/resource_dashboard/
├── README.md
├── dashboard_app.py          # local desktop monitor when a GUI display is available
├── dashboard_server.py       # browser/server fallback for containers or remote hosts
└── static/
    ├── index.html
    ├── app.js
    └── style.css
```

## Build/check the Rust agent

Run from the repository root:

```bash
cargo check --manifest-path instance/resource_agent/Cargo.toml
```

## Browser dashboard, recommended in containers

For Docker containers and remote machines, the browser dashboard is the most reliable choice because it does not require the container to access the host graphical display.

```bash
python3 instance/resource_dashboard/dashboard_server.py \
  --dashboard-listen 0.0.0.0:9202 \
  --agent-listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

Open in the host browser:

```text
http://127.0.0.1:9202
```

If the container is not running with host networking, expose the port when starting the container:

```bash
-p 9202:9202
```

Useful API calls:

```bash
curl -sS http://127.0.0.1:9202/api/health | python3 -m json.tool
curl -sS http://127.0.0.1:9202/api/snapshot | python3 -m json.tool
curl -sS http://127.0.0.1:9202/api/agent/status | python3 -m json.tool
curl -sS -X POST http://127.0.0.1:9202/api/agent/start | python3 -m json.tool
curl -sS -X POST http://127.0.0.1:9202/api/agent/stop | python3 -m json.tool
```

## Desktop dashboard

The desktop dashboard opens a local Tkinter window:

```bash
python3 instance/resource_dashboard/dashboard_app.py \
  --agent-listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

It auto-starts the Rust Resource Agent unless `--no-auto-start` is used. By default, it starts:

```bash
cargo run --manifest-path instance/resource_agent/Cargo.toml -- \
  --listen 127.0.0.1:9201 \
  --sample-interval-ms 1000 \
  --instance-id hp_127.0.0.1:9001
```

Use `--no-auto-start` when another process already owns the Resource Agent lifecycle.

## Demo resource reporting

For end-to-end Proxy resource-state validation, prefer the demo path:

```bash
cd test
python3 demo_proxy.py --host 127.0.0.1 --port 8001 --strategy round_robin --injection-strategy iws
```

In another terminal:

```bash
cd test
python3 demo_instance.py --host 127.0.0.1 --port 9001 --proxy-cp-url http://127.0.0.1:8002
```

Then inspect Proxy-side resource state:

```bash
curl -sS "http://127.0.0.1:8002/debug/instance_resources" | python3 -m json.tool
```

`demo_instance.py` starts or reuses the Rust agent, waits for health, reports snapshots only after registration succeeds, and cleans up only the Resource Agent process group it started.

## Why a container may not open a desktop window

Having a physical monitor on the host machine is not enough. A Docker container cannot automatically access the host graphical display. If the container does not have `DISPLAY` and the X11 socket mounted, Tkinter may fail with:

```text
no display name and no $DISPLAY environment variable
```

Use the browser dashboard, or start the container with X11 forwarding.

### X11 example on Linux hosts

On the host:

```bash
xhost +local:docker
```

Start the container with `DISPLAY` and the X11 socket:

```bash
sudo docker run --gpus all -it \
  --name cacheroute-dev \
  --network host \
  --ipc=host \
  --shm-size=64g \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  --memory=0 \
  --memory-swap=0 \
  -e DISPLAY=$DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v /llm-stack:/workspace/llm-stack \
  basic-cu128 bash
```

Inside the container:

```bash
echo $DISPLAY
python3 - <<'EOF'
import tkinter
print("tkinter: ok")
EOF
```

If this is inconvenient, use `dashboard_server.py` instead. If the container cannot open the desktop window, you can also try running the Rust agent and desktop dashboard directly on the host machine, as long as the host has a compatible Rust/Cargo toolchain and repository permissions are correct.

## Validate direct Rust agent endpoint

```bash
curl -sS http://127.0.0.1:9201/healthz
curl -sS http://127.0.0.1:9201/v1/resource/snapshot | python3 -m json.tool
```

A valid snapshot includes:

```text
schema_version
timestamp_ms
devices.cpu
devices.memory
devices.network
devices.gpu, optional
capacity_hint.admission_state
```

## Troubleshooting

### `cargo: command not found`

Use the CacheRoute Docker image built from `env/docker/Dockerfile`, or install Rust manually.

### `ModuleNotFoundError: No module named 'tkinter'`

Install Tkinter as a system package. It should **not** be added to `requirements.txt`.

```bash
apt-get update
apt-get install -y python3-tk
```

For Python 3.12, use `python3.12-tk` if available.

### Dashboard starts but snapshot is unavailable

Check whether the agent is reachable:

```bash
curl -sS http://127.0.0.1:9201/healthz
```

### GPU list is empty

Check GPU visibility:

```bash
nvidia-smi
```

Make sure the container was started with `--gpus all`.

### Port is already in use

Change the agent or dashboard port:

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

## Future work

- Improve the desktop UI for many-GPU machines.
- Add richer per-Instance queue and KVCache metrics once they are exported by Instance.
- Replace `nvidia-smi` polling with a lower-overhead GPU collector such as NVML.
- Support multi-Instance comparison in one dashboard.

## Integrated Instance Resource Dashboard

`demo_instance.py` can optionally start the browser Resource Dashboard as part of the same Instance process:

```bash
python3 test/demo_instance.py --ui
```

With default settings, the Dashboard listens on `0.0.0.0:9202` and the usable local URL printed by the demo is:

```text
http://127.0.0.1:9202
```

The listen address `0.0.0.0` means the server accepts connections on all container or host interfaces. For local health checks and browser opening, `demo_instance.py` prints and opens `127.0.0.1` instead because wildcard addresses are not usable browser destinations. In containers without host networking, expose the Dashboard port, for example `-p 9202:9202`.

To choose the Dashboard listen address:

```bash
python3 test/demo_instance.py \
  --ui \
  --ui-listen 0.0.0.0:9202
```

Explicit `--ui` opens the local default browser after the Dashboard is ready. In headless containers, remote shells, or CI, disable browser opening while still serving the Dashboard:

```bash
python3 test/demo_instance.py \
  --ui \
  --no-ui-open-browser
```

Use `--no-ui` to disable integrated Dashboard startup entirely, even if `INSTANCE_UI_ENABLE=1` is set. The related environment variables are `INSTANCE_UI_ENABLE`, `INSTANCE_UI_LISTEN`, `INSTANCE_UI_OPEN_BROWSER`, and `INSTANCE_UI_START_TIMEOUT_S`; explicit CLI options take precedence over environment values.

Integrated mode connects the Dashboard to the same Resource Agent listen address and sample interval used by `demo_instance.py`. It always launches `instance/resource_dashboard/dashboard_server.py` with `--no-auto-start`, so `demo_instance.py` remains the only owner of the Resource Agent in the combined workflow. `demo_instance.py` also owns and cleans up only the Dashboard process it starts; if an already reachable Dashboard is reused, it is not terminated on Instance shutdown.

Dashboard startup, readiness, and browser-opening failures are warnings only. They do not stop the Instance; check the `[demo_instance][ui]` log lines for the generated command, readiness timeout or early-exit reason, and bounded stdout/stderr tails.
