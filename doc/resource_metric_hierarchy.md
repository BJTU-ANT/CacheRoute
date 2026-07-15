# Resource metric hierarchy and Scheduler pool-resource contract

Issue #110 keeps Scheduler routing unchanged and clarifies which Proxy pool metrics are safe to report upward. The guiding rule is:

- Proxy may keep detailed per-Instance and queue state for local debugging and future local selection.
- Scheduler receives only compact Proxy-pool summaries and stores them for visibility.
- `0` means a metric was measured as zero. `null` means the metric is unavailable, unwired, or lacks enough source data.

## Scheduler-facing `pool_resource` fields

| Field | Source | Classification | Scheduler semantics |
|---|---|---|---|
| `instances.total` | `InstancePool` registered item count | runtime-maintained counter | Total known Instances, alive or stale. |
| `instances.alive` | `InstancePool.last_seen_at` + TTL | derived runtime state | Alive Instances only. |
| `instances.stale` | `InstancePool.last_seen_at` + TTL | derived runtime state | Registered but TTL-expired Instances. |
| `instances.with_resource` | `InstanceResource.raw_resource` / `resource_reported_at` | derived from measured data | Alive Instances that have at least one resource snapshot. |
| `instances.missing_resource` | alive minus with-resource | derived from measured data | Alive Instances without resource snapshots. |
| `load.inflight_total` | `InstanceLoad.inflight` | runtime-maintained counter when heartbeat supplies it; otherwise unavailable | `null` until Instance heartbeat reports inflight. |
| `load.qps_1m_total` | `InstanceLoad.qps_1m` | runtime-maintained counter when heartbeat supplies it; otherwise unavailable | `null` until Instance heartbeat reports QPS. |
| `load.load_ratio` | `inflight_total / capacity` | derived from source metrics | `null` when inflight or capacity is unavailable. |
| `load.capacity` | `PROXY_MAX_CAPACITY` | static configured value | `null` when unset or non-positive. |
| `load.prepare_queue_depth` | `QueueManager` prepare queue sizes | runtime-maintained counter | Coarse Proxy-level total, not per-Instance detail. |
| `load.ready_queue_depth` | `QueueManager` ready queue sizes | runtime-maintained counter | Coarse Proxy-level total, not per-Instance detail. |
| `load.queue_pressure` | queue depth divided by capacity | derived from source metrics | `null` when queue depth or capacity is unavailable. |
| `utilization.cpu_avg/max` | Instance resource snapshots | real-time measured by lower resource reports | `null` when no resource snapshot contains CPU data. |
| `utilization.memory_*` | Instance resource snapshots | real-time measured by lower resource reports | Aggregated pool-level ratios only. |
| `utilization.gpu_util_*` | Instance resource snapshots | real-time measured by lower resource reports | Scheduler uses resource-derived GPU utilization, not placeholder heartbeat GPU load. |
| `utilization.gpu_mem_*` | Instance resource snapshots | real-time measured by lower resource reports | Aggregated pool-level ratios only. |
| `utilization.network_*` | Instance resource snapshots | real-time measured by lower resource reports | Total RX/TX only when at least one resource report contains the value. |
| `health.resource_freshness_s_*` | `resource_reported_at` | derived from measured data | Shows age of resource snapshots. |
| `health.pool_admission_state` | resource snapshot `capacity_hint.admission_state` | derived from lower-layer admission hints | Coarse state: `accepting`, `degraded`, or `rejecting`. |
| `metric_source` | Proxy aggregation logic | diagnostic metadata | Identifies the active source or `unavailable`. |
| `metric_quality` | Proxy aggregation logic | diagnostic metadata | Indicates `complete`, `partial`, or `missing`. |

## Metrics that remain Proxy-local

The Scheduler-facing summary intentionally excludes raw per-Instance resource payloads, per-task trace fields, predictor internals, and per-Instance queue rows. Those details remain available through Proxy debug APIs such as `/v1/instance/list`, `/debug/instance_resources`, and `/debug/pool_resource_sources`.

## Validation checklist

1. Inspect Proxy-local aggregation:
   ```bash
   curl -sS http://127.0.0.1:8002/debug/pool_resource | python3 -m json.tool
   curl -sS http://127.0.0.1:8002/debug/pool_resource_sources | python3 -m json.tool
   ```
2. Inspect Scheduler storage:
   ```bash
   curl -sS http://127.0.0.1:7002/debug/proxy_pool_resources | python3 -m json.tool
   ```
3. Confirm missing metrics are `null` and marked `unavailable`, not silently reported as measured zero.
4. Confirm Scheduler routing behavior is unchanged; these fields are stored and exposed only.
