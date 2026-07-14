# TTFT Predictor

`TTFT_predictor` estimates and continuously calibrates TTFT (Time To First Token) for LLM requests.

In CacheRoute, TTFT is mainly used as a scheduler-side approximation of the prefill stage. The predictor provides a fast regression model for online decisions, plus warmup and feedback paths that let the model adapt to the actual vLLM deployment.

---

## 1. What this component is for

This directory targets two related scenarios:

1. **Fast prediction before scheduling**: estimate TTFT for a request with a given `batch_size` and `prompt_length`.
2. **Online calibration after execution**: feed measured prefill or first-token latency back into the model so predictions track the real system.

The implementation is intentionally lightweight. It favors a small linear model that can be called frequently by the scheduler over a heavy model that would add prediction latency.

---

## 2. Key files

| File | Role |
| --- | --- |
| `prefill_regressor.py` | Owns the regression model, stores training samples, triggers warmup requests, fits coefficients, and predicts TTFT. |
| `prefill_predictor.py` | Async singleton facade used by Python callers; exposes prediction, feedback update, and detailed warmup APIs. |
| `prefill_prediction_server.py` | FastAPI service exposing `/predict` and `/report_prefill` for out-of-process scheduler integration. |
| `request_generator.py` | Generates tokenizer-controlled prompts and sends measurement requests to vLLM. |
| `local_test.py` | Local helpers for direct TTFT measurement and debugging. |
| `WORKFLOW.md` | Additional call-flow notes for startup, warmup, prediction, and reporting paths. |

---

## 3. Regression model

The predictor fits TTFT with a simple feature set:

```text
TTFT ~= a * (batch_size * prompt_length) + b * prompt_length + c * batch_size + d
```

The terms have the following interpretation:

| Term | Meaning |
| --- | --- |
| `batch_size * prompt_length` | Approximate total prefill work in the batch. |
| `prompt_length` | Per-request context length. |
| `batch_size` | Batch-size overhead and scheduling effects. |
| `d` | Intercept for fixed overhead. |

The implementation normalizes features with `StandardScaler`, fits a `LinearRegression`, and then de-normalizes the coefficients into `a`, `b`, `c`, and `d` for inspection.

This model is only an approximation. It is most useful after warmup on the same model, hardware, vLLM configuration, and request path that will be used in production experiments.

---

## 4. Default configuration

The default vLLM target is defined in `prefill_predictor.py`:

```python
VLLM_CONFIG_DEFAULT = {
    "host": "0.0.0.0",
    "port": 8000,
    "model_id": "llama3-70b",
    "tokenizer_path": "/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct/",
    "batch_sample_policy": "mid_minmax",
}
```

The warmup grid is:

```python
BATCH_SIZES_TO_TEST = range(1, 9)
TOKEN_LENGTHS_TO_TEST = range(64, 2048, 64)
```

`WARM_UP_CONFIGS_DEFAULT` keeps only configurations with `batch_size * prompt_length <= 10000`.

Before running on a new server, verify at least:

- `host` and `port` point to the target vLLM service,
- `model_id` matches the model served by vLLM,
- `tokenizer_path` can be loaded locally,
- the warmup grid fits the GPU memory and expected serving range.

---

## 5. Python module usage

The main Python facade is `prefill_predictor.py`.

### 5.1 Predict TTFT

```python
import asyncio
from prefill_predictor import predict_ttft


async def main():
    prediction = await predict_ttft(batch_size=4, prompt_length=1024)
    print(f"predicted_ttft = {prediction:.4f}s")


asyncio.run(main())
```

`predict_ttft(...)` returns seconds. If the model has not been fitted yet, it falls back to a simple cold-start estimate.

### 5.2 Report measured data

```python
import asyncio
from prefill_predictor import update_prefill_data


async def main():
    await update_prefill_data(
        batch_size=4,
        prompt_length=1024,
        prefill_time=0.185,
    )


asyncio.run(main())
```

`update_prefill_data(...)` appends one training sample to the in-memory regressor. The current validation filters out invalid or extreme values below `0.001s` or above `60s`.

### 5.3 Run detailed warmup

```python
import asyncio
from prefill_predictor import perform_detailed_warmup


async def main():
    await perform_detailed_warmup(repeats=3)


asyncio.run(main())
```

Detailed warmup clears existing training data, tests each `(batch_size, prompt_length)` configuration, collects repeated measurements, and fits the model once all configurations finish.

---

## 6. HTTP prediction service

Run the FastAPI service from the predictor directory:

```bash
cd instance/TTFT_predictor
python prefill_prediction_server.py
```

The current direct-run entry point starts uvicorn with:

```text
host = 172.18.0.250
port = 9003
```

For production-style deployment, prefer an explicit uvicorn command so host, port, workers, and reload behavior are controlled outside the source file:

```bash
uvicorn prefill_prediction_server:app --host 0.0.0.0 --port 9003
```

### 6.1 Health check

```bash
curl http://127.0.0.1:9003/
```

Response shape:

```json
{
  "status": "ok",
  "message": "TTFT Predictor is running."
}
```

### 6.2 Predict endpoint

```bash
curl -X POST http://127.0.0.1:9003/predict \
  -H "Content-Type: application/json" \
  -d '{"batch_size": 4, "prompt_length": 1024}'
```

Response shape:

```json
{
  "predicted_ttft_seconds": 0.185,
  "predicted_ttft_ms": 185.0
}
```

### 6.3 Feedback endpoint

```bash
curl -X POST http://127.0.0.1:9003/report_prefill \
  -H "Content-Type: application/json" \
  -d '{"batch_size": 4, "prompt_length": 1024, "prefill_time_seconds": 0.185}'
```

Response shape:

```json
{
  "status": "received",
  "msg": "Data queued for model update"
}
```

If `batch_size` is omitted from `/report_prefill`, the service currently defaults it to `1` before adding the sample.

---

## 7. Runtime flow

When `prefill_prediction_server.py` starts:

1. The FastAPI lifespan hook calls `predict_ttft(batch_size=1, prompt_length=1)`.
2. This initializes the singleton `PrefillTimeRegressor` and seeds it with a few dummy samples for fast cold start.
3. A background task waits briefly so the HTTP service can become available.
4. The background task runs `perform_detailed_warmup(repeats=3)`.
5. `/predict` remains available while warmup is running.
6. `/report_prefill` can asynchronously add real feedback samples.

This design avoids blocking service startup on a full benchmark while still allowing the model to become more accurate after warmup and real traffic feedback.

---

## 8. Warmup details

### 8.1 Lightweight cold start

The first call to `get_regressor()` creates the regressor and seeds it with a small dummy dataset. This gives the scheduler a usable estimate immediately, but the values should not be treated as calibrated.

Use this state only for:

- service startup,
- smoke tests,
- early scheduling before measured data is available.

### 8.2 Detailed warmup

`perform_detailed_warmup(...)` is the recommended calibration path. It:

1. clears old samples,
2. iterates through `WARM_UP_CONFIGS_DEFAULT`,
3. sends real requests through `request_generator.py`,
4. collects repeated TTFT samples,
5. rewrites the collected samples to the current `(batch_size, prompt_length)` configuration,
6. fits the regression model after all configurations complete,
7. prints fitted coefficients.

The default `repeats=3` is a balance between startup cost and robustness. Increase repeats for smoother coefficients; reduce repeats when you only need a quick approximate model.

---

## 9. Batch sample policy

`prefill_regressor.py` can summarize a batch of concurrent TTFT measurements with different policies through `batch_sample_policy`:

| Policy | Training sample value |
| --- | --- |
| `mid_minmax` | `(min_ttft + max_ttft) / 2`; current default. |
| `mean_ttft` or unknown value | Average of valid per-request TTFT values. |
| `max_arrival` | Maximum first-token arrival offset within the round. |
| `max_ttft` | Maximum valid per-request TTFT. |
| `min_ttft` | Minimum valid per-request TTFT. |

`mid_minmax` is useful when a batch contains mild pseudo-serialization and the midpoint between fastest and slowest requests is a more stable training target than either extreme.

---

## 10. Scheduler integration pattern

A typical CacheRoute integration loop is:

1. Before dispatch, estimate the prefill stage with `/predict` or `predict_ttft(...)`.
2. Use the estimate in queueing, batching, or routing decisions.
3. After the request reaches first token, compute the measured prefill/TTFT value.
4. Report the measurement through `/report_prefill` or `update_prefill_data(...)`.
5. Periodically rerun detailed warmup after major deployment changes.

This keeps the predictor fast enough for online scheduling while still correcting drift from real traffic.

---

## 11. Troubleshooting

### First predictions are inaccurate

The model is probably still using dummy cold-start data or an incomplete warmup. Run detailed warmup and keep feeding measured values from real requests.

### Prediction is zero or extremely small

Common causes:

- no fitted model yet,
- invalid input values,
- too little training data,
- reported samples filtered out because they are below `0.001s` or above `60s`.

### Warmup takes too long

Reduce one or more of:

- maximum `batch_size`,
- maximum `prompt_length`,
- `repeats`,
- or the number of tested configurations.

The default grid is broad and may be expensive on smaller GPUs.

### Prompt length does not exactly match the target

`request_generator.py` uses the tokenizer to approximate prompts with a target token count. Small deviations are expected and usually acceptable for warmup.

### Batch behavior looks unstable

Check request dispatch gaps and the selected `batch_sample_policy`. Large dispatch gaps can prevent requests from landing in the same effective batch and may distort fitted coefficients.

---

## 12. Quick start checklist

1. Edit `VLLM_CONFIG_DEFAULT` for your vLLM host, port, model id, and tokenizer path.
2. Start the service:

   ```bash
   cd instance/TTFT_predictor
   python prefill_prediction_server.py
   ```

3. Check health:

   ```bash
   curl http://127.0.0.1:9003/
   ```

4. Run a prediction:

   ```bash
   curl -X POST http://127.0.0.1:9003/predict \
     -H "Content-Type: application/json" \
     -d '{"batch_size": 1, "prompt_length": 1024}'
   ```

5. Feed back real measurements through `/report_prefill` after requests complete.
6. Re-run warmup after changing model, vLLM configuration, hardware, tensor parallelism, or scheduling policy.

---

## 13. Relationship to TPOT prediction

TTFT prediction models the cost before the first generated token, which is dominated by prefill and request setup. TPOT prediction models the per-token decode stage after the first token. Scheduler-side latency estimates usually need both:

```text
total_generation_latency ~= predicted_TTFT + predicted_decode_time
```

Use this directory for prefill/first-token prediction and `instance/TPOT_predictor` for decode-token prediction.
