# Pool Resource Metric Contract

Issue #110 defines the first Scheduler-facing contract for `pool_resource`.
The Scheduler stores this snapshot and exposes it for debugging; it must not use
these fields for routing until a later scheduling strategy explicitly opts in.

## Granularity

Proxy keeps detailed per-Instance state locally in `/v1/instance/list` and
`/debug/instance_resources`. Scheduler receives only a coarse Proxy-pool summary:
instance counts, aggregate load, aggregate utilization, pool admission state, and
metric provenance.

## Null vs zero

For Scheduler-facing fields:

- `0` means a metric source reported a real zero.
- `null` means the metric is unavailable, not wired, or unknown.
- Missing fields remain tolerated for backward compatibility.

This prevents downstream policies from treating placeholders as idle capacity.

## Current field audit

| Field | Source | Classification | Scheduler contract |
| --- | --- | --- | --- |
| `instances.total` | Proxy `InstancePool` registry | runtime-maintained counter | Reported count. |
| `instances.alive` | Proxy TTL check using `last_seen_at` | derived from runtime state | Reported count. |
| `instances.stale` | Proxy TTL check using `last_seen_at` | derived from runtime state | Reported count. |
| `instances.with_resource` | Instances with accepted resource snapshots | derived from measured data | Reported count. |
| `instances.missing_resource` | Alive minus resource-reporting instances | derived from measured data | Reported count. |
| `load.inflight_total` | Optional Instance heartbeat `inflight` | runtime-maintained when supplied; otherwise unavailable | Sum when present, otherwise `null`. |
| `load.qps_1m_total` | Optional Instance heartbeat `qps_1m` | runtime-maintained when supplied; otherwise unavailable | Sum when present, otherwise `null`. |
| `load.load_ratio` | `inflight_total / capacity` | derived from load and static config | `null` unless inflight is available and capacity is positive. |
| `load.capacity` | Proxy `PROXY_MAX_CAPACITY` / register-time config | static configured value | Always reported as configured integer. |
| `load.prepare_queue_depth` | Proxy `QueueManager` aggregate prepare queue size | runtime-maintained queue counter | Reported when data-plane queue manager is available; otherwise `null`. |
| `load.ready_queue_depth` | Proxy `QueueManager` aggregate ready queue size | runtime-maintained queue counter | Reported when data-plane queue manager is available; otherwise `null`. |
| `load.queue_pressure` | queue depth divided by capacity | derived from queue counters and static config | `null` unless queue depth and capacity are available. |
| `utilization.cpu_avg/max` | Instance resource-agent snapshots | real-time measured snapshot aggregate | `null` when no resource snapshots exist. |
| `utilization.memory_*` | Instance resource-agent snapshots | real-time measured snapshot aggregate | `null` when not reported by snapshots. |
| `utilization.gpu_util_*` | Instance resource-agent GPU snapshots | real-time measured snapshot aggregate | `null` when not reported by snapshots. |
| `utilization.gpu_mem_*` | Instance resource-agent GPU snapshots | real-time measured snapshot aggregate | `null` when not reported by snapshots. |
| `utilization.network_*` | Instance resource-agent network snapshots | real-time measured snapshot aggregate | `null` when not reported by snapshots. |
| `pool_admission_state` | Instance resource snapshot `capacity_hint.admission_state` plus missing-resource state | derived from measured data | Coarse accepting/degraded/rejecting summary. |
| `resource_freshness_s` | Snapshot receive time in Proxy | derived from measured data | min/avg/max age; all `null` without snapshots. |

## Provenance metadata

Each `pool_resource` includes:

- `metric_source`: field-level source labels such as `instance_resource_snapshot`,
  `proxy_queue_manager`, `proxy_config`, or `unavailable`.
- `metric_quality`: coarse `complete`, `partial`, or `missing` labels for
  resource, load, and queue groups.

Proxy also exposes `/debug/pool_resource_sources` for a compact diagnostic view.
Scheduler exposes `/debug/proxy_pool_resources` and stores these metadata fields
without interpreting them.
