# AGENTS.md

author: heyao
version-date: 26-03-15

## 1. Project Overview

This repository implements a CacheRoute-style scheduling system for LLM serving experiments.

The system currently revolves around the following roles:

- **Scheduler**: the central control plane that selects target Proxy/KDN resources and builds requests.
- **Proxy**: executes or forwards requests, maintains runtime load information, and interacts with the scheduler.
- **KDN**: manages knowledge-related assets such as text chunks, KVCache, embeddings, and resource status.
- **Client / Perf Client**: generates workloads for testing and benchmarking.

The project is actively evolving. The current focus is not “general cleanup”, but **supporting research-driven scheduling strategies and controlled experiments**.

Codex should optimize for:
- correctness,
- incremental change,
- observability,
- reproducible experiments,
- and preserving existing behavior unless explicitly asked to change it.

### 1.2 Architecture Overview

Typical task flow:

client
  ↓
scheduler
  ↓
proxy
  ↓
instance
  ↓
vLLM engine

KDN provides knowledge resources used during scheduling
and request preparation.

Scheduler maintains resource pools for:
- proxies
- KDN nodes

### 1.3 CacheRoute Strategy Development Area

Scheduling strategies should be implemented under:

scheduler/strategy/
proxy/strategy/

Strategies must not modify core scheduler logic.
They should operate through the existing selection interfaces.

Example strategies include:
- round_robin
- load_based
- knowledge_aware
- hybrid_injection_policy

---

## 2. Current Research Direction

This repository is being extended for **strategy development under different task injection modes**.

The main task modes currently relevant are:

- **text**: send text directly for normal computation / recomputation.
- **kvcache**: inject KVCache-related information into the workflow.
- **hybrid**: mixed mode, e.g. alternating or ratio-based combinations of text and kvcache tasks.

The near-term research goal is to support **decision-making between KVCache injection and text-recomputation dynamically**, based on:
- queue state,
- network cost,
- compute cost,
- and resource availability.

When modifying code, prefer solutions that help future policy design, especially:
- measurable timing breakdowns,
- explicit request metadata,
- stable task typing,
- and resource-state visibility.

---

## 3. Engineering Priorities

When making changes, follow these priorities in order:

1. **Do not break the existing experiment flow.**
2. **Prefer incremental modifications over broad refactors.**
3. **Preserve existing public behavior unless the task explicitly requires changes.**
4. **Expose useful logs / debug status for validation.**
5. **Keep code easy to reason about for later paper-oriented modeling.**

Do **not** do the following unless explicitly requested:
- rewrite large modules,
- rename core concepts casually,
- move many files at once,
- introduce new abstractions without clear payoff,
- silently change request field semantics,
- remove debug outputs that are useful for experiments.

---

## 4. Expected Coding Style

### General
- Use **small, local, incremental patches**.
- Keep the original project structure unless there is a strong reason not to.
- Prefer **clear active-voice comments** over decorative comments.
- Avoid clever but opaque designs.

### Python style
- Preserve the repository’s existing style where possible.
- Add type hints when they improve clarity, but do not over-engineer typing.
- Keep functions focused and readable.
- Avoid introducing unnecessary framework-level indirection.

### Logging / Observability
- Add logs only when they help validate runtime behavior.
- Prefer logs that answer questions like:
  - which task was selected,
  - which injection type was used,
  - how long each phase took,
  - which proxy/KDN was chosen,
  - what resource/load snapshot was used.

Avoid noisy per-step logs unless explicitly requested. Favor **task-level summary logs**.

---

## 5. Change Strategy for Codex

When implementing a request, Codex should usually follow this workflow:

1. **Read the relevant files first.**
2. **Infer the minimum viable change set.**
3. **Modify only the files needed for that change.**
4. **Explain the patch in a file-by-file manner.**
5. **State how to verify the change.**
6. **Call out assumptions explicitly if code context is incomplete.**

For non-trivial changes, the preferred response format is:

- **What to change**
- **Why this is the minimal correct change**
- **Exact code patch**
- **How to validate**
- **What may break / edge cases**

---

## 6. Validation Expectations

Every meaningful code change should be easy to validate.

Codex should prefer adding or preserving validation hooks such as:
- debug endpoints,
- CLI-visible status,
- task-level timing output,
- resource pool snapshots,
- counters for success/failure events,
- explicit request field dumps in controlled logs.

When proposing changes, always include:
- where to run,
- what to send,
- what output is expected,
- what would indicate failure.

Do not stop at “the code compiles”. Runtime validation matters.

---

## 7. Important Domain Conventions

### 7.1 Injection type
A request may carry an `Injection_type` field.
If absent, default behavior should remain compatible with the current system expectation (currently usually `kvcache`, unless the target code path says otherwise).

Supported modes may include:
- `kvcache`
- `text`
- `hybrid`

Do not hard-code new mode semantics in many places. Centralize branching logic where practical.

### 7.2 Hybrid mode
Hybrid mode may be ratio-driven rather than a fixed pattern.
For example:
- 2 KVCache + 1 text
- 3 KVCache + 1 text

When implementing hybrid logic, favor parameterized behavior over one-off hardcoding.

### 7.3 Timing model relevance
Some timing fields are not just operational metrics; they are research data.
Preserve or improve the ability to separate:
- proxy queue time,
- waiting time,
- knowledge acquisition time,
- network-related delay,
- actual compute/prefill time.

Avoid collapsing these phases if they are currently distinguishable.

### 7.4 Resource pools
Scheduler decisions may depend on resource pools rather than transient heartbeat payloads alone.
If a load metric is already maintained in a pool object, prefer reading from the maintained state instead of reconstructing it elsewhere.

---

## 8. KDN / Knowledge-Related Expectations

KDN-related data may include:
- text registration,
- KVCache readiness,
- embeddings,
- status fields such as `kv_ready`,
- knowledge identifiers (`kid`),
- and resource visibility from scheduler/CLI.

When changing KDN paths:
- do not assume registration means all derived states are already consistent,
- do not silently skip re-registration logic if the experimental workflow requires overwrite/refresh,
- keep status observability available for debugging.

If a change affects knowledge registration or scheduler-KDN interaction, include explicit validation steps.

---

## 9. Performance Experiment Expectations

This codebase is used for controlled experiments, not only production-style serving.

Therefore, Codex should prefer features that support:
- reproducible workload generation,
- JSON-driven workloads,
- concise per-task performance summaries,
- average / min / max statistics when useful,
- and low-friction experimentation across injection modes.

For client-side or perf tooling:
- avoid burying important metrics in overly verbose logs,
- prefer one-line-per-task summaries plus aggregate statistics.

---

## 10. Backward Compatibility Rules

Unless explicitly requested otherwise:

- keep existing request formats working,
- keep existing CLI/debug workflows usable,
- keep current default values stable,
- avoid changing field names already used by scheduler/proxy/client,
- and avoid changing wire semantics without documenting them.

If a backward-incompatible change is unavoidable, clearly mark:
- old behavior,
- new behavior,
- migration points,
- and the minimal files that must be updated together.

---

## 11. What Codex Should Do When Context Is Incomplete

If the requested change depends on files or symbols not yet inspected:

- do not invent repository structure,
- do not fabricate function names,
- do not pretend a patch is exact if it is only conceptual.

Instead:
- say which file(s) must be inspected,
- give a likely patch location,
- and separate **confirmed changes** from **assumption-based suggestions**.

Accuracy is more important than sounding complete.

---

## 12. Preferred Response Style for This Repository

When assisting with code changes in this repo, prefer:

- concrete edits,
- exact insertion points,
- minimal diffs,
- explicit reasoning,
- and validation commands/examples.

Avoid:
- generic architecture lectures,
- unnecessary abstraction proposals,
- large speculative rewrites,
- or answers that ignore the current code state.

The user typically prefers **incremental, directly actionable code guidance**.

---

## 13. If You Add New Code

When introducing a new helper, field, or branch:
- keep naming consistent with existing repository terms,
- add comments for non-obvious logic,
- avoid spreading one new concept across many files unless necessary,
- and ensure logs/debug outputs make the new behavior observable.

If adding config/arguments:
- use existing configuration style where possible,
- document defaults,
- and make the feature easy to disable.

---

## 14. Key Files / Entry Points

The exact filenames below are important. Prefer editing these files in place instead of introducing parallel logic elsewhere.

- `scheduler/...`: request building, scheduling decisions, resource selection
- `proxy/...`: request execution path, runtime load updates, queue behavior
- `kdn/...`: knowledge registration, status fields, embedding / KVCache readiness
- `instance/...`: interact layer between proxy and vLLM engine.
- `client.py`: single-request testing path
- `perf_client.py`: workload generation and benchmark execution
- `demo_kdn.py`,`demo_scheduler.py`,`demo_proxy.py`,`demo_instance.py`,`demo_client.py`: startup arguments only; do not place business logic here

---

## 15. Do Not Disturb Without Explicit Request

Unless explicitly requested, do not redesign:
- scheduler/proxy/KDN role boundaries
- existing request wire format
- client/perf_client invocation style
- current CLI/debug interfaces
- default injection behavior

---

## 16. Preferred Patch Delivery Format

For code assistance, prefer:
1. exact target file
2. exact function/class location
3. minimal patch
4. explanation of why this location is correct
5. validation steps

Do not provide only high-level pseudocode when the code context is already available.

---

## 17. Summary

This repository is a research-driven scheduling/serving system under active iteration.

The most helpful contributions are:
- precise incremental patches,
- stable semantics,
- strong observability,
- and implementation choices that support future scheduling strategy research.

When in doubt, choose:
**minimal change + clear validation + preserved compatibility**.