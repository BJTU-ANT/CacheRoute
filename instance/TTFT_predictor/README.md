# TTFT Predictor

`TTFT_predictor` 用于估计和在线校准 LLM 请求的 `TTFT`（Time To First Token，首 token 延迟）。

这个目录里的代码主要面向两类场景：

1. 在调度器里快速预测在给定 `batch_size` 和 `prompt_length` 下的 TTFT。
2. 通过真实请求对预测模型做 warmup 和在线数据回流更新。

当前实现：

- `prefill_regressor.py` + `prefill_predictor.py` + `prefill_prediction_server.py`


## 目录结构

- `[prefill_regressor.py]`：线性回归器，负责收集训练数据、拟合模型、发起 warmup 请求和执行预测。
- `[prefill_predictor.py]`：对外提供异步接口，管理单例回归器，支持预测、数据回流和详细 warmup。
- `[prefill_prediction_server.py]`：FastAPI 服务，对外暴露 HTTP 接口。
- `[request_generator.py]`：根据目标 token 数生成 prompt，并向 vLLM 发送测试请求。
- `[local_test.py]`：本地测量 TTFT 的辅助函数。

## 核心思路

模型使用一个简单的线性特征组合来拟合 TTFT：

`TTFT ~= a * (batch_size * prompt_length) + b * prompt_length + c * batch_size + d`

其中：

- `batch_size * prompt_length` 反映 prefill 计算量
- `prompt_length` 反映单请求上下文长度
- `batch_size` 反映批大小本身带来的调度/排队影响

这一近似模型的优点是：

- 推理非常快，适合调度器实时调用
- 易于通过在线采样持续校准
- 很适合做 warmup 后的近似预测

## 工作流程文档

函数调用关系、服务启动链路、后台预热线路、`/predict` 与 `/report_prefill` 的触发路径，已经单独整理在：

`[WORKFLOW.md]`

## 依赖

建议的 Python 依赖至少包括：

```bash
pip install numpy aiohttp scikit-learn transformers fastapi uvicorn pydantic
```

如果你在 Linux 服务器上运行，还需要保证：

- 可以访问目标 vLLM 服务
- `tokenizer_path` 对应模型的 tokenizer 可加载

## 使用方式

### 方式 1：作为 Python 模块直接调用

最常用的接口在 `[prefill_predictor.py]`。

可用接口：

- `predict_ttft(batch_size, prompt_length)`：预测 TTFT，返回秒
- `update_prefill_data(batch_size, prompt_length, prefill_time)`：上报真实 prefill 时间
- `perform_detailed_warmup(...)`：执行真实 warmup 和模型拟合

示例：

```python
import asyncio
from prefill_predictor import predict_ttft, update_prefill_data

async def main():
    pred = await predict_ttft(batch_size=4, prompt_length=1024)
    print(f"predicted ttft = {pred:.4f}s")

    await update_prefill_data(
        batch_size=4,
        prompt_length=1024,
        prefill_time=0.185
    )

asyncio.run(main())
```

说明：

- 第一次调用 `predict_ttft` 时会初始化回归器
- 如果还没有真实 warmup 数据，会先用少量 dummy data 启动
- 后续可以持续调用 `update_prefill_data` 做在线校准

### 方式 2：启动 HTTP 预测服务

如果你希望调度器通过 HTTP 调用预测服务，直接运行：

```bash
cd example/scheduler/TTFT_predictor
python prefill_prediction_server.py
```

默认行为：

- 服务启动时先做一次轻量初始化
- 然后在后台异步执行真实 warmup
- 预测接口在 warmup 未完成前也可以先返回一个可用结果

默认服务地址写在代码里：

- host: `172.18.0.250`
- port: `9000`

如果要改部署地址，直接修改 `[prefill_prediction_server.py](/d:/研/代码/PD分离推理调度代码/BurstGPT/example/scheduler/TTFT_predictor/prefill_prediction_server.py)` 末尾的 `uvicorn.run(...)` 即可。

## HTTP 接口说明

### 1. 健康检查

`GET /`

返回示例：

```json
{
  "status": "ok",
  "message": "TTFT Predictor is running."
}
```

### 2. TTFT 预测

`POST /predict`

请求体：

```json
{
  "batch_size": 4,
  "prompt_length": 1024
}
```

返回示例：

```json
{
  "predicted_ttft_seconds": 0.183,
  "predicted_ttft_ms": 183.0
}
```

### 3. 回流真实 prefill 数据

`POST /report_prefill`

请求体：

```json
{
  "batch_size": 4,
  "prompt_length": 1024,
  "prefill_time_seconds": 0.176
}
```

返回示例：

```json
{
  "status": "received",
  "msg": "Data queued for model update"
}
```

这个接口适合在真实推理完成后，把测得的 prefill 时间回传给预测器。

## Warmup 使用说明

### 轻量 warmup

`prefill_predictor.py` 在首次初始化时会先注入少量 dummy data，优点是启动快，缺点是精度一般。

适合场景：

- 服务刚启动
- 只想尽快得到一个大致可用的 TTFT 估计

### 详细 warmup

更推荐的方式是执行 `perform_detailed_warmup(...)`，它会：

1. 遍历一组 `(batch_size, prompt_length)` 配置
2. 通过 `request_generator.py` 发送真实请求
3. 等待外部将真实 prefill 时间回流到回归器
4. 用采集到的数据重新拟合模型

默认测试范围定义在 `[prefill_predictor.py]`：

- `BATCH_SIZES_TO_TEST = range(1, 9)`
- `TOKEN_LENGTHS_TO_TEST = range(64, 2048, 64)`
- 并过滤 `bs * pl <= 10000`

你可以直接修改：

- `VLLM_CONFIG_DEFAULT`
- `WARM_UP_CONFIGS_DEFAULT`

来适配你的模型、服务地址和测试范围。

## 配置项说明

### `VLLM_CONFIG_DEFAULT`

位于 `[prefill_predictor.py]`。

主要字段：

- `host`：目标 vLLM 服务地址
- `port`：目标 vLLM 服务端口
- `model_id`：请求时使用的模型标识
- `tokenizer_path`：本地 tokenizer 路径

### `WARM_UP_CONFIGS_DEFAULT`

表示 warmup 时要测试的 `(batch_size, prompt_length)` 组合。

如果你模型更大或者机器更弱，建议适当缩小范围，例如减少：

- 最大 `batch_size`
- 最大 `prompt_length`
- `repeats`

否则 warmup 时间会明显变长。

## 与调度器集成建议

如果要把它接到调度器里，推荐使用下面的模式：

1. 调度前调用 `/predict` 或 `predict_ttft(...)`
2. 根据预测结果做请求分配或排队决策
3. 请求完成后，把真实 prefill 时间通过 `/report_prefill` 回流
4. 周期性或启动后执行一次 `perform_detailed_warmup(...)`

这样预测器既能快速返回，又能随着真实流量持续修正。

## 常见问题

### 1. 为什么第一次预测不准

因为模型刚启动时通常只用了 dummy data，还没有完成真实 warmup。

建议：

- 启动后先执行一次详细 warmup
- 或者让系统运行一段时间，持续回流真实 prefill 数据

### 2. 为什么 warmup 触发了请求，但没有数据

`trigger_warmup_requests(...)` 只负责发请求，不会自动测量真实 prefill 时间并写回。

也就是说，你需要有外部逻辑调用：

- `update_prefill_data(...)`
- 或 `/report_prefill`

否则只会有请求，没有训练数据。

### 3. 为什么预测值会是 0 或非常小

常见原因：

- 模型还没拟合成功
- 输入数据非法
- 上报的 `prefill_time_seconds` 被过滤掉了

当前过滤逻辑会丢弃：

- 小于 `0.001s`
- 大于 `60s`

### 4. 为什么 prompt 长度和真实 token 数不完全一致

`request_generator.py` 是根据 tokenizer 近似生成目标 token 数的 prompt，实际生成文本可能会有轻微偏差。这对 warmup 一般是可接受的。

## 快速开始

### 直接启动服务

```bash
cd example/scheduler/TTFT_predictor
python prefill_prediction_server.py
```

### 调用预测接口

```bash
curl -X POST http://172.18.0.250:9000/predict \
  -H "Content-Type: application/json" \
  -d "{\"batch_size\": 4, \"prompt_length\": 1024}"
```

### 回流真实 prefill 数据

```bash
curl -X POST http://172.18.0.250:9000/report_prefill \
  -H "Content-Type: application/json" \
  -d "{\"batch_size\": 4, \"prompt_length\": 1024, \"prefill_time_seconds\": 0.176}"
```

## 版本说明

当前目录已经统一到 `prefill_*` 系列，文档、服务接口和调度器接入都以这一套实现为准。
