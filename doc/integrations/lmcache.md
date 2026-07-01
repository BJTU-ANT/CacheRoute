# CacheRoute + LMCache Integration

CacheRoute uses LMCache as the KV cache backend for reusable knowledge injection across LLM systems.

## Why LMCache?

LMCache provides KV cache storage and reuse support for LLM inference engines. CacheRoute builds a scheduling layer on top of LMCache to decide when and where KVCache-based knowledge injection should be used.

## What CacheRoute Adds

- Knowledge-oriented task routing across LLM systems.
- Compute-network-aware injection strategy selection.
- KDN-based registration and injection of reusable knowledge KVCache blocks.
- OpenAI-compatible request forwarding through the Scheduler.

## Tested Environment

| Component | Version |
|---|---|
| vLLM | 0.13.x |
| LMCache | 0.3.x |
| Redis | 7 |
| Python | 3.12.11 |

## Demo

See the main README for Quick Start and performance overview.
