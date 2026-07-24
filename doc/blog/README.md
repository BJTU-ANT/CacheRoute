# CacheRoute Blog / Engineering Log

This document records major engineering milestones for the CacheRoute prototype. Detailed debugging notes, implementation discussions, review comments, and task ownership should be tracked in GitHub Issues and Pull Requests.

## How to Read This Log

- Newer entries are listed first.
- Each entry uses a consistent structure: context, changes, validation, follow-ups, and owner.
- This file is a curated engineering log, not a replacement for component-level READMEs.
- For runnable commands and current usage, prefer the README closest to the relevant component.

---

## 260724: Reproducible Setup, Client Command Parsing, and Integrated Instance Dashboard

### Changes

- Reworked the container and deployment guides around a reproducible `/llm-stack` workflow, modern NVIDIA Container Toolkit setup, headless/Tkinter separation, and a complete single-machine startup runbook.
- Declared a compatible `redis-py` dependency to prevent legacy Redis client import failures in the KDN KV builder.
- Added a shared multiline curl-like command parser for the CLI and browser client, including URL-first and standard curl forms, line continuations, clearer quote errors, and regression tests.
- Integrated optional Resource Dashboard startup into `demo_instance.py` with readiness/reuse checks, browser launch control, process ownership and cleanup, focused tests, and updated Instance documentation.

### Files

- `README.md`
- `env/README.md`
- `env/docker/Dockerfile`
- `requirements.txt`
- `client/command_input.py`
- `client/repl.py`
- `UI/client_ui/`
- `test/demo_instance.py`
- `instance/instance_api.py`
- `instance/resource_dashboard/`

Owner: yao

---

## 260723: Predictive Queue Pressure and Interactive Proxy Topology

### Changes

- Added per-Instance predicted pressure from queue reservations and timelines, including pending Prefill work, active Decode work, slot readiness, Prefill availability, and estimated backlog time.
- Extended `least_load` scoring and `/debug/instance_loads` to use the same predictive pressure snapshot while preserving instantaneous, inflight-only, and round-robin fallback behavior.
- Upgraded the Proxy topology to a deterministic force-relaxed layout with stable positions, curved links, particles, hover relationships, drag/pan/zoom controls, fit/reset actions, and reduced-motion support.
- Expanded pure-function tests for scheduling pressure, topology determinism, bounds, collisions, link geometry, tooltips, and interaction helpers.

### Files

- `proxy/queue/manager.py`
- `proxy/strategy/least_load.py`
- `proxy/proxy.py`
- `proxy/resource/instance_pool.py`
- `UI/proxy_ui/static/`
- `UI/proxy_ui/test_proxy_ui_pure.js`

Owner: yao

---

## 260720: Queue-Aware Least-Load Scheduling and Proxy UI Visualization

### Changes

- Added Proxy-maintained per-Instance `inflight` lifecycle counters and `/debug/instance_loads` for consistent load-aware selection and diagnostics.
- Extended `least_load` scoring with active prepare/ready work and prepare/ready queue depths, while preserving unknown-metric handling and round-robin fallback semantics.
- Rebuilt the Proxy UI topology as a responsive ONOS-style SVG and added a visual Instance detail dashboard with KPIs, resource bars, GPU/memory charts, queue/load views, accessibility, and raw-data diagnostics.
- Added an LMCache configuration reference and simplified README request examples for lightweight validation.

### Files

- `proxy/strategy/least_load.py`
- `proxy/proxy.py`
- `proxy/resource/instance_pool.py`
- `proxy/resource/p_control_plane.py`
- `UI/proxy_ui/`
- `doc/LMCache_config_para.md`
- `README.md`
- `client/README.md`

Owner: yao

---

## 260716: Least-Load Instance Selection Strategy

### Changes

- Added an experimental `least_load` Proxy Instance selection strategy using `load.inflight` as the primary signal and `load.qps_1m` as a fallback.
- Preserved unknown metrics as unknown and fall back to round-robin when no usable load information is available.
- Added round-robin tie-breaking and strategy aliases without changing the default `round_robin` behavior.
- Documented CLI and environment configuration for reproducible load-aware scheduling experiments.

### Files

- `proxy/strategy/least_load.py`
- `proxy/strategy/factory.py`
- `proxy/README.md`

Owner: yao

---

## 260715: Proxy Pool Resource Reporting and Metric Contract

### Changes

- Added compact Proxy `pool_resource` aggregation and reporting to Scheduler registration and heartbeat flows.
- Added Scheduler-side storage and debug APIs for Proxy resource state, including liveness, freshness, utilization, load, and admission summaries.
- Defined null-vs-zero semantics, metric provenance and quality fields, and coarse prepare/ready queue-pressure reporting.
- Added a required PR template and Codex branch-hygiene workflow to keep generated changes scoped and reviewable.

### Files

- `proxy/resource/instance_pool.py`
- `proxy/resource/p_control_plane.py`
- `proxy/queue/`
- `scheduler/resource/`
- `doc/pool_resource_metrics.md`
- `.github/PULL_REQUEST_TEMPLATE.md`
- `doc/codex_workflow.md`

Owner: yao

---

## 260714: Proxy UI and Repository Readability Improvements

### Changes

- Added a browser-based Proxy observability UI with Instance status, resource snapshots, topology state, and a compact `/debug/status` API.
- Integrated Proxy UI startup and cleanup into `test/demo_proxy.py`, and documented frontend entry URLs.
- Expanded TTFT/TPOT predictor documentation and improved request, tokenizer, prompt-generation, and non-blocking warmup workflows.
- Standardized English comments, docstrings, logs, and README content across the main CacheRoute modules.

### Files

- `UI/proxy_ui/`
- `proxy/resource/p_control_plane.py`
- `test/demo_proxy.py`
- `instance/TTFT_predictor/`
- `instance/TPOT_predictor/`
- project source comments and READMEs

Owner: yao

---

## 260713: Instance Resource Agent Demo Integration and Resource Reporting

### Context

The goal of this milestone was to move Instance resource monitoring from a manual flow to a demo-ready flow. Before this work, users needed to start the Rust Resource Agent manually and optionally run a separate reporter. After this milestone, `test/demo_instance.py` can own the Resource Agent lifecycle and report resource snapshots to the Proxy after Instance registration.

### Changes

- `test/demo_instance.py` enables resource monitoring by default for demos.
- The demo Instance explicitly owns the Rust Resource Agent lifecycle:
  - feature flag resolution;
  - process startup or reuse;
  - `/healthz` readiness checks;
  - periodic snapshot reporting;
  - shutdown cleanup;
  - process-group `SIGTERM` / `SIGKILL` fallback.
- Resource reporting starts only after Instance registration with the Proxy succeeds, avoiding repeated `unknown_instance` reports.
- `instance/resource_agent/proxy_reporter.py` adds report metadata:
  - `reported_instance_id`;
  - `report_monotonic_ms`;
  - `report_wall_time_ms`;
  - `agent_snapshot_timestamp_ms`.
- Proxy `InstancePool.resource` now stores normalized CPU, memory, GPU, network, admission-state, and timestamp fields.
- Proxy resource inspection APIs are documented and available:
  - `GET /debug/instance_resources`;
  - `GET /v1/instance/list?include_dead=true`.
- Proxy resource snapshot success logs are reduced: the first successful report remains `INFO`, repeated successful updates move to `DEBUG`.
- `test/demo_resource_monitor_e2e.py` was added to start Proxy + Instance, wait for several resource reports, terminate the Instance, and verify that the demo-owned Resource Agent is cleaned up.
- `test/README.md` was added to explain the purpose of demo and test scripts under `test/`.

### Validation

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

Inspect resource state from the Proxy control plane:

```bash
curl -sS "http://127.0.0.1:8002/debug/instance_resources" | python3 -m json.tool
curl -sS "http://127.0.0.1:8002/v1/instance/list?include_dead=true" | python3 -m json.tool
```

Inspect the Rust Resource Agent directly:

```bash
curl -sS http://127.0.0.1:9201/healthz
curl -sS http://127.0.0.1:9201/v1/resource/snapshot | python3 -m json.tool
```

Run the e2e smoke script with a non-default agent port:

```bash
python3 test/demo_resource_monitor_e2e.py \
  --agent-listen 127.0.0.1:19201 \
  --agent-url http://127.0.0.1:19201
```

### Current Status

Issue #86 is functionally complete. The demo Instance can start or reuse a Resource Agent, register with the Proxy, report resource snapshots after registration, and expose the resource state through Proxy APIs.

The resource state is still observational. Proxy Instance selection does not yet use resource fields.

### Follow-ups

1. Design resource-aware Instance selection using `InstancePool.resource`.
2. Add finer-grained Instance metrics, such as queue state, KVCache block residency, and vLLM runtime state.
3. Replace `nvidia-smi` polling with a lower-overhead GPU collection path.
4. Continue improving the Resource Dashboard UI for multi-GPU machines.

Owner: yao

---

## 260713: Prototype Roadmap After Global Scheduler and Local Injection Strategy

### Context

The global knowledge-injection Scheduler has a working prototype. The local resource-pool layer has focused mainly on dynamic knowledge-injection decisions. The Instance selection layer still needs to be organized and extended.

<img width="400" alt="image" src="https://github.com/user-attachments/assets/965b2b48-afe2-4c26-8784-ae52f7f4bcbe" />

### Plan

1. Resource-pool-level Instance scheduling.
   - Use the Rust Resource Agent to observe and manage Instance resources.
   - Report Instance resource snapshots to Proxy through API or gRPC.
   - Let Proxy maintain Instance resource and KVCache state, then report useful summaries to KDN / Scheduler when needed.
2. KDN Server improvements.
   - The current KDN Server supports knowledge registration, query, and feedback.
   - A future KDN control plane should maintain knowledge resources more precisely and support KVCache placement strategy.

Owner: yao

---

## 260312: Perf Client Improvements, LMCache Key Investigation, and KDN Network Model

### Changes

- Investigated why LMCache keys could not be reused across container cycles.
- Confirmed that `Today-Date` information in the chat template changes the chunk hash and invalidates KVCache prefixes.
- Extended `perf_client.py` with hybrid-mode `Injection_type` support to test mixed injection strategies.
- Improved the KDN Server network model:
  - requests inside one batch can run concurrently;
  - bandwidth is shared within a batch;
  - batches are transferred sequentially;
  - batch window and related variables are configurable.

### Files

- `model/tokenizer_config.json`
- `client/perf_client.py`
- `kdn_server/kv_injector.py`
- `kdn_server/kdn_api.py`
- `test/demo_kdn.py`

### Follow-ups

- Build a readable KDN UI.
- Build an Instance-side resource inspection platform.
- Maintain pool-level business flow state with dual inflight accounting.
- Update the knowledge manifest with live LLM-system state.

Owner: yao

---

## 260311: Client Display and Load Generation Improvements

### Changes

- Added task timestamp display to the client.
- Extended `perf_client.py` and simplified `workload.json`.
- Added `client/taskset/` for batch task JSON generation.
- Added RPS mode to `perf_client.py` for continuous load tests and performance curve experiments.

### Files

- `client/taskset/`
- `client/client.py`
- `client/perf_client.py`

Owner: yao

---

## 260310: KDN Batch Registration and CLI Display Improvements

### Changes

- Added batch knowledge-text registration from JSON files to initialize a KDN knowledge base.
- Improved KDN CLI output so users can inspect overall resource-pool state.

### Files

- `kdn_server/util/knowledge_manifest.json`
- `kdn_server/util/batch_register_kdn.py`
- `kdn_server/kdn_api.py`
- `kdn_server/kdn_register_cli.py`
- `kdn_server/text_db.py`

Owner: yao

---

## 260309: Parallel Proxy Task Queues, Timing Traces, and Client Load Tools

### Changes

- Upgraded the previous serial `prepare-ready` flow to a queue plus prepare concurrency model.
- Added dispatchers and multiple prepare workers for each Instance.
- Added task timing anchors in epoch milliseconds.
- Added `cacheroute_meta` to chat / completion results for performance tracing.
- Added `perf_client.py` and `workload.json` for continuous load generation.
- Supported forwarding `Injection_type` as a temporary strategy-control field while Proxy policies were still evolving.
- Continued LMCache key reuse investigation.

### Example

```bash
python3 perf_client.py \
  --base-url http://127.0.0.1:7001 \
  --workload-file workload.json \
  --requests 2 \
  --concurrency 8 \
  --allow-duplicate \
  --seed 7
```

### Files

- `proxy/queue/__init__.py`
- `proxy/queue/task.py`
- `proxy/queue/manager.py`
- `proxy/queue/knowledge.py`
- `proxy/queue/instance_queues.py`
- `client/perf_client.py`
- `workload.json`
- `core/request.py`
- `scheduler/scheduler.py`
- `proxy/proxy.py`

Owner: yao

---

## 260306: v0.1.4 Text and KVCache Injection End-to-End Path

### Changes

- Implemented KVCache-based knowledge injection.
- When `Injection_type=kvcache`, Proxy first classifies and prepares text knowledge, then asks the selected Instance to inject KVCache into local Redis.
- Proxy waits for the Instance ACK before moving the task into the ready queue.
- `proxy.manager` classifies knowledge needs into `kv_ready`, `text_only`, and `miss`.
- Built the Proxy -> Instance -> KDN Server KV injection subpath.
- Integrated concrete KVCache injection messages and reuse behavior.

### Files

- `instance/kv_service.py`
- `instance/control_plane.py`
- `proxy/queue/task.py`
- `proxy/queue/manager.py`
- `proxy/queue/knowledge.py`
- `instance/instance_api.py`
- `core/config.py`
- `kdn_server/kdn_api.py`

Owner: yao

---

## 260305: Proxy Prepare + Ready Queue Structure

### Changes

- Connected text injection and KVCache injection under a common task flow.
- Added `Injection_type` to mark the intended knowledge injection mode.
- Moved Proxy handler logic from direct `forward_request(...)` to the queue module.
- Added per-Instance `prepare` and `ready` queues.
- Moved knowledge injection work into prepare-queue workers.
- Ready-queue workers forward requests to Instances and return outputs through task response queues.

### Files

- `proxy/queue/__init__.py`
- `proxy/queue/task.py`
- `proxy/queue/manager.py`
- `proxy/queue/knowledge.py`
- `proxy/queue/instance_queues.py`
- `core/request.py`
- `scheduler/scheduler.py`
- `proxy/proxy.py`

Owner: yao

---

## 260304: Scheduler and Proxy Log Noise Reduction

### Changes

- Scheduler heartbeat logs were converted into periodic summaries.
- Proxy heartbeat logs were converted into periodic summaries.
- This reduced terminal noise during long-running experiments.

### Files

- `scheduler/resource/hb_log.py`
- `proxy/resource/hb_log.py`
- `core/config.py`
- `scheduler/scheduler.py`
- `scheduler/resource/control_plane.py`
- `scheduler/knowledge/kdn_sync.py`
- `proxy/proxy.py`

Owner: yao

---

## 260303: Scheduler Proxy-Pool State Maintenance

### Changes

- Proxy registration now reports static capability fields, such as max capacity, Instance count, KVCache memory capacity, and KVCache update policy.
- Scheduler maintains Proxy inflight state with an event-driven stream lifecycle and low-frequency Proxy heartbeat calibration.
- This hybrid design reduces accounting overhead while preventing long-term inflight drift.

### Files

- `core/request.py`
- `core/config.py`
- `scheduler/scheduler.py`
- `scheduler/scheduler_cli.py`
- `scheduler/resource/control_plane.py`
- `scheduler/resource/proxy_pool.py`
- `proxy/proxy.py`
- `proxy/sclient/scheduler_client.py`

Owner: yao

---

## 260302: Scheduler Display Improvements and KDN + Proxy Strategy Integration

### Changes

- `scheduler_cli` status can display KDN resource-pool state.
- KDN refresh was changed to build a new table first and then swap it in, reducing concurrency issues.
- KDN selection strategy was integrated into the Scheduler strategy layer.
- KDN refresh now runs immediately after successful KDN registration.

### Files

- `core/request.py`
- `scheduler/scheduler.py`
- `scheduler/scheduler_cli.py`
- `scheduler/resource/control_plane.py`
- `scheduler/knowledge/kdn_sync.py`
- `scheduler/strategy/base.py`
- `scheduler/strategy/round_robin.py`
- `store/knowledge_base.py`

Owner: yao

---

## 260202: Proxy and Scheduler Pool Structure Improvements

### Changes

- Proxy strategies can now operate on the Instance pool instead of using a hardcoded default.
- Proxy initialization supports strategy loading.
- Scheduler builds a KDN pool and initializes its knowledge manifest from registered KDN servers.
- The startup order was clarified:

```text
1. Start Scheduler. It maintains proxy_pool and kdn_pool and exposes control plane 7002.
2. Start KDN and Proxy. They register with the Scheduler control plane and heartbeat.
3. Start Instance. It binds to a vLLM backend, probes resources, and registers with the local Proxy.
```

### Files

- `core/config.py`
- `scheduler/scheduler.py`
- `scheduler/resource/control_plane.py`
- `scheduler/knowledge/kdn_sync.py`
- `scheduler/resource/kdn_pool.py`
- `kdn_server/sclient/scheduler_client.py`
- `proxy/proxy_cli.py`
- `proxy/README.md`
- `test/demo_kdn.py`
- `README.md`
- `proxy/strategy/base.py`
- `proxy/strategy/factory.py`
- `proxy/strategy/round_robin.py`

Owner: yao

---

## 260201: Proxy CLI Display

### Changes

- Added Proxy CLI support for inspecting Proxy status and the Instance pool.
- Updated Proxy README with usage information.

### Files

- `proxy/proxy_cli.py`
- `proxy/README.md`

Owner: yao

---

## 260131: Instance Usability Improvements

### Changes

- Instances can run on multiple ports, reducing Proxy registration overwrites in multi-Instance demos.
- Proxy and Instance interaction logs were improved.

### Files

- `proxy/resource/p_control_plane.py`
- `instance/instance_api.py`
- `test/demo_instance.py`

Owner: yao

---

## 260130: v0.1.1 Proxy and Instance Control-Plane Integration

### Changes

- Implemented the Proxy control plane with FastAPI on default port `8002`.
- Added `InstancePool` to maintain Instance static state, load fields, and `last_seen` timestamps.
- Proxy lifespan now creates and starts the InstancePool-backed control plane.
- Instance supports Proxy register / heartbeat / unregister.

### Files

- `proxy/proxy.py`
- `core/config.py`
- `instance/instance_api.py`
- `test/demo_instance.py`
- `proxy/resource/instance_pool.py`
- `proxy/resource/p_control_plane.py`
- `instance/pclient/proxy_client.py`

Owner: yao

---

## 260129: Proxy Lifecycle and Scheduler Client

### Changes

- Proxy registers with Scheduler during startup, heartbeats periodically, and unregisters on exit.
- Added `proxy/sclient` for Scheduler control-plane communication.
- Added `proxy/metrics` as a placeholder for later local resource aggregation.
- Clarified the Proxy dual-plane structure:
  - service plane: default `8001`;
  - control plane: default `8002`.

### Files

- `proxy/proxy.py`
- `core/config.py`
- `proxy/sclient/scheduler_client.py`
- `proxy/metrics/local_metrics.py`

Owner: yao

---

## 260128: Scheduler Control Plane, Proxy Pool, and Strategy Skeleton

### Changes

- Added a Scheduler-side Proxy pool with static and dynamic fields.
- Scheduler now starts a service plane, resource pool, and control plane.
- Implemented a round-robin scheduling strategy.
- Added `--strategy` to `demo_scheduler.py`.
- Moved scheduling decision flow into `Request.build_request`.
- Expanded Scheduler CLI functionality.

### Files

- `core/config.py`
- `core/request.py`
- `scheduler/resource/control_plane.py`
- `scheduler/scheduler.py`
- `scheduler/scheduler_cli.py`
- `test/demo_scheduler.py`
- `scheduler/resource/proxy_pool.py`
- `scheduler/strategy/base.py`
- `scheduler/strategy/factory.py`
- `scheduler/strategy/round_robin.py`

Owner: yao

---

## 260127: Documentation, Scheduler Control Plane, and Startup Scripts

### Changes

- Updated `env/README.md` with vLLM + LMCache image build steps.
- Added startup scripts for container and multi-terminal testing.
- Moved `demo_scheduler.py` local config parameters into `core/config.py`.
- Reorganized Scheduler directories around `knowledge` and `resource`.
- Added Scheduler control-plane APIs for Proxy interaction and resource synchronization.

### Files

- `env/README.md`
- `core/config.py`
- `test/demo_scheduler.py`
- `scheduler/scheduler.py`
- `test/quick_start_docker.sh`
- `scheduler/resource/control_plane.py`

Owners: chen, yao

---

## 260126: v0.1.0 Scheduler / Proxy / Request Knowledge-Management Refactor

### Changes

- KDN `/search/text` supports field-level responses.
- KDN `/snapshot` returns full knowledge-base state.
- Scheduler initializes its knowledge manifest from KDN snapshots instead of a local YAML-only preset.
- `knowledge_base` supports SHA256-to-int64 mapping for FAISS lookup compatibility.
- Scheduler CLI was improved.
- Scheduler supports dynamic KDN knowledge synchronization with a two-stage incremental refresh.

### Files

- `kdn_server/text_db.py`
- `kdn_server/kdn_api.py`
- `scheduler/__init__.py`
- `scheduler/scheduler.py`
- `scheduler/kdn_client.py`
- `scheduler/scheduler_cli`
- `store/knowledge_base.py`
- `core/request.py`
- `proxy/proxy.py`
- `test/demo_scheduler.py`
- `scheduler/kdn_sync.py`

Owner: yao

---

## 260123: KDN Server Feature Improvements

### Changes

- Added `kv_builder` state fields so text blocks can indicate whether KVCache has been built.
- Extended SQLite schema with KV metadata and backfilled it after KV build completion.
- Integrated text and KVCache registration/query operations into `kdn_register_cli.py`.

### Files

- `kdn_server/text_db.py`
- `kdn_server/kdn_api.py`
- `kdn_server/kv_builder.py`
- `kdn_server/kdn_register_cli.py`
- `scheduler/kdn_client.py`

Owner: yao

---

## 260121: KDN Server Data Structures

### Changes

- Standardized KDN knowledge-block storage and naming.
- Generated unique IDs from text hashes.
- Added SQLite indexing.
- Built a KVCache store that can persist CacheGen-compressed KVCache from Redis.
- Implemented KVCache reinjection into Redis.
- Maintained the KDN Server README.

### Files

- `test/demo_kdn.py`
- `kdn_server/kdn_api.py`
- `kdn_server/text_db.py`
- `kdn_server/kdn_register_cli.py`
- `kdn_server/kv_builder.py`
- `kdn_server/kv_injector.py`
- `util/kdn_build_kv.py`

Owner: yao

---

## 260120: System Polish

### Changes

- Improved streaming display in `client.py`.
- Fixed Proxy knowledge injection behavior for chat and completion modes.

### Files

- `client/client.py`
- `proxy/proxy.py`

Owner: yao