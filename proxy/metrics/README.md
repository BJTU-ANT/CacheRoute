### Queue Predictor

The queue predictor estimates the time from when the proxy places a task into an instance queue until the first token response is received. It specifically includes prefill time and queue waiting time.

After writing data into `ttft_benchmark_table.json`, run `python3 ttft_four_term_regressor.py` to fit the prediction model and automatically write the parameters into `ttft_coefficient.json`.

Quick validation of regression effectiveness:

```bash
python3 ttft_four_term_regressor.py
```

### Redis Pull-Time Regression

When you have an experiment table like `kvcache_size_gb, redis_pull_ms_1..N`, run the following command (CSV/JSON supported):

```bash
python3 proxy/metrics/redis_pull_regressor.py --input your_data.csv
```

It writes linear coefficients in milliseconds to `proxy/metrics/redis_pull_coefficients.json`:

```json
{"intercept_ms": 0.0, "slope_ms_per_gb": 0.0}
```

Example JSON input format:

```json
[
  {"kvcache_size_gb": 0.5, "redis_pull_ms_1": 10.0, "redis_pull_ms_2": 11.0},
  {"kvcache_size_gb": 1.0, "redis_pull_ms_1": 18.0, "redis_pull_ms_2": 19.0}
]
```

The following JSON structures are also supported:
- A top-level array: `[ {...}, {...} ]`.
- A top-level object whose sample key is `rows`, `data`, or `samples`.

If a sample has no `kvcache_size_gb` but has `actual_hit_length_tokens`, add:

```bash
--kv-gb-per-token 0.000001
```

In that case, the fitter automatically converts with `kvcache_size_gb = actual_hit_length_tokens * kv_gb_per_token` before fitting.

The predictor side can call directly:

```python
from proxy.metrics.queue_predictor import queue_predictor
```

Unified prediction convention (recommended):

```bash
python3 proxy/metrics/queue_predictor.py --length 1024 --knowledge-length 512
```

It structurally outputs two scenarios:
- `text-based`: pure compute time estimated by the quartic model from `--length` (that is, total_length).
- `kvcache-based` (when `--knowledge-length` is provided): knowledge hit length (aligned to 256), KVCache size, remaining length to compute, remaining text compute time, Redis pull time, and total pull-plus-remaining-recompute time.
