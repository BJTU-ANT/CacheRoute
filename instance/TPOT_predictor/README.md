# TPOT Predictor

`TPOT_predictor` measures and models TPOT (Time Per Output Token) for vLLM-style streaming generation.

This module is designed for CacheRoute experiments that need decode-time estimates by **real `sequence_length`**, not just by the synthetic prompt length used to generate benchmark requests. It can collect TPOT curves, remove outliers, smooth low-confidence points, fit a four-term decode model, compare baseline and prefill-loaded scenarios, and export length-wise results for later scheduling analysis.

---

## 1. What this predictor measures

TPOT is measured from client-side streaming token arrivals:

- `send_stream_request_for_tpot(...)` records token-event arrival timestamps with `time.perf_counter()`.
- The delay from request dispatch to the first generated token is treated as TTFT.
- The interval between later generated-token events is treated as the TPOT step delta.

Because the measurement boundary is the client stream receiver, TPOT includes more than pure GPU decode time:

- server-side decode execution,
- vLLM streaming flush behavior,
- SSE chunk aggregation,
- network jitter,
- and local event-loop scheduling jitter.

For this reason, the exported TPOT curve should be interpreted as an end-to-end decode-token service curve under the current deployment, not as a hardware-only kernel benchmark.

---

## 2. Key files

| File | Role |
| --- | --- |
| `tpot_predictor.py` | High-level orchestration APIs for collection, range curves, fitting, prediction, scenario comparison, and exports. |
| `tpot_regressor.py` | Stores observations, applies outlier filtering and smoothing, builds length-wise curves, fits the four-term model, and predicts decode time. |
| `request_generator.py` | Builds tokenizer-controlled prompts and sends streaming requests to the target vLLM service. |
| `local_test.py` | Local helpers for ad-hoc measurement and debugging. |
| `output/` | Recommended location for generated JSON, CSV, and XLSX artifacts. |

---

## 3. Default benchmark configuration

The default vLLM target is defined in `tpot_predictor.py`:

```python
VLLM_CONFIG_DEFAULT = {
    "host": "0.0.0.0",
    "port": 8000,
    "model_id": "llama3-70b",
    "tokenizer_path": "/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct/",
}
```

The default test grid is:

```python
BATCH_SIZES_TO_TEST = range(1, 9)
TOKEN_LENGTHS_TO_TEST = [
    *range(8, 128, 8),
    *range(128, 512, 32),
    *range(512, 2048, 64),
]
```

`WARM_UP_CONFIGS_DEFAULT` keeps only configurations with `batch_size * prompt_length <= 10000` to avoid excessively large benchmark requests.

Adjust these defaults before using the predictor on a different model, tokenizer path, host, port, or GPU capacity.

---

## 4. Sequence length semantics

Most public APIs in this directory use **real `sequence_length`** as the user-facing length.

Internally, prompt generation still uses `target_prompt_length`. The predictor estimates the chat-template offset through the tokenizer and maps the requested real sequence-length interval to the prompt lengths that should be tested.

This distinction matters because TPOT during decode depends on the actual KV length seen by the model:

```text
sequence_length = real prompt tokens already in context + generated tokens so far
```

When using the range APIs, `length_start` and `length_end` always refer to this real sequence-length interval.

---

## 5. Main collection modes

### 5.1 Matrix collection

Use `collect_tpot_matrix(...)` when you already know the exact `(batch_size, target_prompt_length)` configurations to test:

```python
regressor = await collect_tpot_matrix(
    configs=[(1, 128), (1, 256), (2, 128)],
    max_tokens=16,
    repeats=3,
    concurrency=None,
)
```

This mode is useful for controlled warmup grids and low-level regression debugging.

### 5.2 Range collection by real sequence length

Use `collect_tpot_range(...)` when the scheduler or experiment logic needs a continuous curve over a real sequence-length interval:

```python
result = await collect_tpot_range(
    batch_sizes=[1, 2, 4],
    length_start=128,
    length_end=512,
    max_tokens=16,
    repeats=3,
    fit_after_collect=True,
)
```

The returned dictionary includes:

- `requested`: the user-facing range and batch sizes,
- `planned_test_configs`: the internally generated `(batch_size, target_prompt_length)` tests,
- `fit_coefficients`: the fitted four-term model coefficients when fitting succeeds,
- `range_curve`: the final length-wise rows,
- `summary`: aggregate benchmark diagnostics,
- `regressor`: the live `TPOTRegressor` instance.

### 5.3 Continuous curve collection for one batch size

Use `collect_continuous_tpot_curve(...)` when you want one request to cover several adjacent decode positions and then tile the requested interval with overlapping windows:

```python
result = await collect_continuous_tpot_curve(
    batch_size=1,
    real_input_length=128,
    length_start=128,
    length_end=256,
    max_tokens=32,
    repeats=3,
    overlap_tokens=4,
    fit_after_collect=True,
)
```

This mode is usually the most convenient way to build smooth per-length curves because each streaming request contributes TPOT observations for:

```text
sequence_length = L0, L0 + 1, ..., L0 + max_tokens - 1
```

Overlapping windows reduce missing points and make interpolation less fragile.

---

## 6. Robust statistics and diagnostics

Streaming TPOT data often contains spikes. Common causes include SSE event aggregation, short network stalls, client event-loop scheduling delay, and low sample counts for individual `(batch_size, sequence_length)` buckets.

The predictor keeps several diagnostic columns instead of hiding this uncertainty:

| Column | Meaning |
| --- | --- |
| `raw_samples` | Number of raw TPOT deltas collected for this length. |
| `filtered_samples` | Number of samples left after outlier filtering. |
| `raw_mean_tpot_ms` | Mean TPOT before filtering. |
| `filtered_mean_tpot_ms` | Mean TPOT after filtering. |
| `filtered_median_tpot_ms` | Median TPOT after filtering; preferred as the default stable statistic. |
| `filtered_p95_tpot_ms` | P95 TPOT after filtering. |
| `outlier_count` | Number of samples removed by the filter. |
| `is_low_confidence` | Whether the point has fewer samples than `min_samples_for_filter`. |
| `suspicious_spike` | Whether a low-confidence point is unusually high relative to neighbors. |
| `smoothed_tpot_ms` | Sliding-median smoothing result within the same batch size. |
| `default_tpot_ms` | Main value used for fitting and prediction. |
| `value_source` | `observed`, `interpolated`, `fitted`, or `none`. |

By default, `default_tpot_ms` is selected in this priority order:

1. `filtered_median_tpot_ms`,
2. `smoothed_tpot_ms`,
3. `filtered_mean_tpot_ms`.

This policy keeps the raw and filtered values visible while making downstream fitting less sensitive to one-off spikes.

---

## 7. Four-term decode fitting and prediction

After collection, the predictor can fit a four-term regression model over the length-wise curve:

```python
coeffs = fit_tpot_four_term(regressor, label_key="default_tpot_ms")
```

The high-level range APIs can also fit automatically with `fit_after_collect=True`.

To estimate the total decode time for a future request, use:

```python
prediction = predict_decode_time(
    regressor=regressor,
    batch_size=1,
    start_sequence_length=128,
    max_tokens=32,
    prefer_fitted=True,
    label_key="default_tpot_ms",
)
```

The return value comes from `TPOTRegressor.predict_decode_time_ms(...)` and is intended to support scheduling-level estimates of how long the decode phase will occupy the instance.

---

## 8. Coverage inspection

Use `summarize_results(...)` for a readable text summary:

```python
print(summarize_results(summary, full_curve_bs=1, length_range=(128, 192)))
```

Use `check_length_coverage(...)` to verify whether a range is fully covered:

```python
coverage = check_length_coverage(
    regressor,
    batch_size=1,
    length_start=128,
    length_end=192,
)
```

Typical fields include:

- `covered_lengths`,
- `missing_lengths`,
- `coverage_ratio`,
- `max_gap`.

If the coverage ratio is low, increase `max_tokens`, increase overlap, add more starting windows, or use a denser collection range.

---

## 9. Export formats

The regressor can export JSON and length-wise curves:

```python
regressor.export_json("instance/TPOT_predictor/output/tpot_results.json")
regressor.export_lengthwise_curve(
    "instance/TPOT_predictor/output/tpot_length_curve.csv",
    rows=result["range_curve"],
)
```

Supported curve file extensions include `.csv`, `.json`, and `.xlsx`. If `.xlsx` is requested but `openpyxl` is not installed, the implementation falls back to CSV.

Recommended export naming pattern:

```text
output/{scenario}_bs{batch_size}_{length_start}_{length_end}.{csv|json|xlsx}
```

---

## 10. Prefill-load interference experiments

The TPOT predictor can compare decode behavior in two scenarios:

- `baseline`: normal TPOT collection,
- `with_prefill_load`: TPOT collection while background prefill-style requests are sent.

The prefill load is approximated with long prompts and `max_tokens=1`:

```python
loaded = await collect_continuous_tpot_curve(
    batch_size=1,
    real_input_length=33,
    length_start=33,
    length_end=256,
    repeats=1,
    max_tokens=32,
    with_prefill_load=True,
    prefill_prompt_length=1024,
    prefill_concurrency=1,
    prefill_interval_ms=0,
    prefill_max_tokens=1,
)
```

This does not represent a strict prefill-only primitive, because the OpenAI-compatible chat/completions API usually still produces at least one token. It is best interpreted as a practical interference workload for studying decode latency under prefill pressure.

Compare scenarios with:

```python
compare_rows = compare_tpot_between_scenarios(
    regressor=loaded["regressor"],
    batch_size=1,
    length_start=33,
    length_end=256,
    value_key="default_tpot_ms",
)

export_scenario_compare(
    regressor=loaded["regressor"],
    output_path="instance/TPOT_predictor/output/compare_bs1_33_256.csv",
    rows=compare_rows,
)
```

---

## 11. Minimal end-to-end example

```python
import asyncio
from tpot_predictor import collect_continuous_tpot_curve, summarize_results


async def main():
    result = await collect_continuous_tpot_curve(
        batch_size=1,
        real_input_length=128,
        length_start=128,
        length_end=192,
        max_tokens=16,
        repeats=3,
        outlier_method="mad",
        outlier_threshold=3.5,
        min_samples_for_filter=5,
        smooth_window=5,
        spike_ratio_threshold=1.8,
        fit_after_collect=True,
    )

    summary = result["summary"]
    print(summarize_results(summary, full_curve_bs=1, length_range=(128, 192)))

    regressor = result["regressor"]
    regressor.export_lengthwise_curve(
        "instance/TPOT_predictor/output/range_128_192_bs1.xlsx",
        rows=result["range_curve"],
    )


asyncio.run(main())
```

Run it from the repository root or make sure this directory is on `PYTHONPATH` so that local imports such as `request_generator` and `tpot_regressor` resolve correctly.

---

## 12. Practical tuning advice

- Increase `repeats` when individual length buckets have too few samples.
- Increase `max_tokens` when you want each request to cover a longer sequence-length window.
- Increase `overlap_tokens` when you want smoother continuity between windows.
- Keep `prefer_fitted=True` when the curve has missing points and the fitted model is stable.
- Use `prefer_fitted=False` when you want to inspect observed and interpolated values without model substitution.
- Treat `suspicious_spike=True` points as diagnostics before using them for scheduler tuning.
- Recollect curves when changing model, GPU, tensor parallelism, vLLM version, scheduler policy, or background load.
