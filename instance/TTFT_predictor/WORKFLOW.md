# TTFT Predictor Workflow

这一节重点说明：

- 程序启动后会调用哪些函数
- 这些函数内部又会触发哪些函数
- 外部请求进入后会走哪条链路
- 真实 prefill 数据回流后会更新哪里

## 1. 服务启动流程

当你运行：

```bash
python prefill_prediction_server.py
```

入口位于 `[prefill_prediction_server.py](/d:/研/代码/PD分离推理调度代码/BurstGPT/example/scheduler/TTFT_predictor/prefill_prediction_server.py)`。

启动后主流程如下：

1. `uvicorn.run("prefill_prediction_server:app", ...)`
2. FastAPI 创建应用并进入 `lifespan(app)`
3. `lifespan()` 内首先调用：

```python
await predict_ttft(batch_size=1, prompt_length=1)
```

这个调用的目的不是做真实预测，而是触发预测器单例初始化。

## 2. 首次初始化会触发哪些函数

`predict_ttft(...)` 定义在 `[prefill_predictor.py](/d:/研/代码/PD分离推理调度代码/BurstGPT/example/scheduler/TTFT_predictor/prefill_predictor.py)`。

调用链如下：

1. `predict_ttft(batch_size, prompt_length)`
2. `get_regressor()`
3. 如果 `_regressor is None`：
   `PrefillTimeRegressor()`
4. 注入 dummy data：
   `add_data(...)`
5. 使用 dummy data 做一次拟合：
   `fit()`
6. 返回到 `predict_ttft(...)`
7. 调用：
   `regressor.predict(batch_size, prompt_length)`

也就是说，服务启动时一定会先触发：

- `get_regressor()`
- `PrefillTimeRegressor.__init__()`
- 多次 `add_data(...)`
- `fit()`
- `predict(...)`

这一阶段不会触发真实网络压测，只会在内存中初始化一个可用模型。

## 3. 启动后的后台真实预热流程

在 `lifespan()` 里，完成上面的 dummy 初始化后，还会继续调度一个后台任务：

```python
asyncio.create_task(run_warmup_in_background())
```

对应调用链如下：

1. `lifespan()`
2. `run_warmup_in_background()`
3. `await asyncio.sleep(5)`
4. `await perform_detailed_warmup(repeats=3)`

也就是说：

- 服务起来后不会立刻跑真实 warmup
- 会先等待 5 秒
- 然后后台异步执行真实 warmup

## 4. 真实 warmup 会触发哪些函数

`perform_detailed_warmup(...)` 位于 `[prefill_predictor.py](/d:/研/代码/PD分离推理调度代码/BurstGPT/example/scheduler/TTFT_predictor/prefill_predictor.py)`。

它的主要流程如下：

1. `perform_detailed_warmup(...)`
2. `get_regressor()`
3. `regressor.clear_data()`
4. 遍历 `WARM_UP_CONFIGS_DEFAULT`
5. 对每个 `(batch_size, prompt_length)` 调用：
   `regressor.trigger_warmup_requests(...)`
6. `trigger_warmup_requests(...)` 内部会：
   `AutoTokenizer.from_pretrained(...)`
7. 为当前配置构造 prompt：
   `generate_prompt_with_tokens(...)`
8. 并发发送请求：
   `send_test_request(...)`
9. warmup 请求发完后，等待数据通过：
   `update_prefill_data(...)`
   回流到 `_training_data`
10. 所有配置结束后调用：
    `fit()`

## 5. 启动后是否会触发 `WARM_UP_CONFIGS_DEFAULT`

会触发。

服务启动后的真实 warmup 最终调用的是：

```python
await perform_detailed_warmup(repeats=3)
```

而 `perform_detailed_warmup(...)` 会逐个遍历：

```python
BATCH_SIZES_TO_TEST = range(1, 9)
TOKEN_LENGTHS_TO_TEST = range(64, 2048, 64)

WARM_UP_CONFIGS_DEFAULT = [
    (bs, pl)
    for bs in BATCH_SIZES_TO_TEST
    for pl in TOKEN_LENGTHS_TO_TEST
    if bs * pl <= 10000
]
```

所以这段配置在服务启动后的后台真实 warmup 中会被使用。

但要区分两个阶段：

- 启动时立即执行的 `predict_ttft(1, 1)`：不会用到 `WARM_UP_CONFIGS_DEFAULT`
- 5 秒后的后台 `perform_detailed_warmup(...)`：会用到 `WARM_UP_CONFIGS_DEFAULT`

## 6. 外部 `/predict` 请求会触发哪些函数

当外部发送：

```http
POST /predict
```

请求体例如：

```json
{
  "batch_size": 4,
  "prompt_length": 1024
}
```

对应调用链如下：

1. `handle_prediction(request)`
2. `predict_ttft(batch_size, prompt_length)`
3. `get_regressor()`
4. 如果模型已经初始化：
   `regressor.predict(batch_size, prompt_length)`

所以 `/predict` 的作用是：

- 获取当前模型
- 根据当前回归器状态返回一个 TTFT 预测值

它不会主动触发 warmup，也不会主动发送测试请求。

## 7. 外部 `/report_prefill` 请求会触发哪些函数

当外部发送：

```http
POST /report_prefill
```

请求体例如：

```json
{
  "batch_size": 4,
  "prompt_length": 1024,
  "prefill_time_seconds": 0.176
}
```

对应调用链如下：

1. `handle_prefill_report(report, background_tasks)`
2. `background_tasks.add_task(update_prefill_data, ...)`
3. FastAPI 在后台执行：
   `update_prefill_data(batch_size, prompt_length, prefill_time)`
4. `update_prefill_data(...)` 内部会：
   `get_regressor()`
5. 然后调用：
   `regressor.add_data(batch_size, prompt_length, prefill_time)`

也就是说，`/report_prefill` 的作用是把真实 prefill 测量值追加到训练缓冲区。

注意：

- 这一步默认只是 `add_data(...)`
- 不会立刻自动重新 `fit()`
- 当前详细 warmup 流程会在收集完一批数据后统一 `fit()`

## 8. 一条完整链路示例

下面是一条典型运行链路：

1. 启动 `prefill_prediction_server.py`
2. `lifespan()` 调用 `predict_ttft(1, 1)`
3. `get_regressor()` 创建 `PrefillTimeRegressor`
4. 用 dummy data 调用 `fit()`
5. 服务开始对外提供 `/predict` 和 `/report_prefill`
6. 后台任务 `run_warmup_in_background()` 等待 5 秒
7. 进入 `perform_detailed_warmup(...)`
8. 按 `WARM_UP_CONFIGS_DEFAULT` 逐个触发 `trigger_warmup_requests(...)`
9. `trigger_warmup_requests(...)` 通过 `generate_prompt_with_tokens(...)` 和 `send_test_request(...)` 向 vLLM 发真实请求
10. 如果外部有真实 prefill 时间上报到 `/report_prefill`
11. `update_prefill_data(...)` 把这些点写入 `_training_data`
12. `perform_detailed_warmup(...)` 收集完后调用 `fit()`
13. 后续 `/predict` 将使用新的拟合模型输出更准确的 TTFT

## 9. 当前流程里一个容易忽略的点

`trigger_warmup_requests(...)` 只负责“发请求”，不负责自动测量和自动回填真实 prefill 时间。

所以如果系统里没有额外逻辑去调用：

- `update_prefill_data(...)`
- 或 `/report_prefill`

那么真实 warmup 可能会把请求发出去，但训练缓冲区里没有足够新数据，最后无法得到理想的拟合效果。
