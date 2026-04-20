# TPOT Predictor

`TPOT_predictor` 参考 `TTFT_predictor` 结构实现，目标是输出 **固定 batch size 下，随真实 sequence length 变化的 TPOT 曲线**，并支持拟合：

\[
TPOT(bs, length) = a1 \cdot bs \cdot length + a2 \cdot bs + a3 \cdot length + a4
\]

---

## 1. 目录结构

- `tpot_predictor.py`：对外异步入口，包含采集、摘要、拟合与 decode 时间预测接口。
- `tpot_regressor.py`：核心采集与聚合，含 `TPOTFourTermRegressor`。
- `request_generator.py`：prompt 构造 + SSE 流式解析 + token 级时间记录。
- `__init__.py`：导出常用接口。

---

## 2. 关键口径

### 2.1 `target_prompt_length` 与 `real_input_length`

实现会同时记录：

- `target_prompt_length`：裸 prompt 目标长度。
- `real_input_length`：`apply_chat_template` 后真实进入模型的 prefill 长度。
- `input_length_offset = real_input_length - target_prompt_length`。

并打印每个 task 的 debug：`target_prompt_length / real_input_length / diff`。

### 2.2 sequence length 定义

对每个生成 step：

- `token_index` 从 1 开始（第一个生成 token）。
- `sequence_length = real_input_length + token_index - 1`。

这表示“生成该 token 前的序列长度”。

### 2.3 最小可观测 length

由于存在 chat template offset，`sequence_length` 的最小值通常不会从 1 起。

输出里会给：

- `min_observed_sequence_length`
- `max_observed_sequence_length`

避免误判为漏记。

---

## 3. 输出组织

### 3.1 原始 records

每条任务保留：`batch_size`, `target_prompt_length`, `real_input_length`, `input_length_offset`, `token_steps`。

### 3.2 按长度曲线（按 bs）

`summary.length_wise_by_bs[*].length_tpot_curve[*]`：

- `sequence_length`
- `samples`
- `mean_tpot_ms`
- `median_tpot_ms`
- `p95_tpot_ms`

### 3.3 单独导出 length-wise 文件

支持导出 CSV/JSON（默认 CSV）：

- `batch_size, sequence_length, samples, mean_tpot_ms, median_tpot_ms, p95_tpot_ms`

---

## 4. 默认采样策略（短段加密）

默认 `TOKEN_LENGTHS_TO_TEST`：

- `8..120` 步长 8
- `128..480` 步长 32
- `512..1984` 步长 64

即短长度加密、长长度放宽。

---

## 5. 拟合模型（四项线性组合）

`TPOTFourTermRegressor` 使用特征：

- `[bs * length, bs, length, 1]`

标签默认：

- `mean_tpot_ms`（也可传 `median_tpot_ms`）

导出系数：

```json
{
  "a1": ...,
  "a2": ...,
  "a3": ...,
  "a4": ...,
  "unit": "ms",
  "source": "TPOTFourTermRegressor"
}
```

---

## 6. Decode 时间计算

接口按：

\[
T_{decode}(l,b,bs)=\sum_{i=0}^{b-1}TPOT(bs,l+i)
\]

逐点求和。

若某个 `length` 无直接样本：

1. 先用同 `bs` 的观测曲线做插值（或边界最近值）；
2. 若无可插值点且 `prefer_fitted=True`，再用四项拟合值；
3. 若仍不可得，兜底为 0。

---

## 7. 使用

```python
import asyncio
from tpot_predictor import collect_tpot_matrix, fit_tpot_four_term, predict_decode_time

async def main():
    reg = await collect_tpot_matrix(configs=[(1, 64), (4, 128)], max_tokens=16, repeats=2)
    coeffs = fit_tpot_four_term(reg, label_key="mean_tpot_ms")
    print(coeffs)

    decode = predict_decode_time(
        regressor=reg,
        batch_size=4,
        start_sequence_length=160,
        max_tokens=32,
        prefer_fitted=True,
    )
    print(decode["total_decode_ms"])

asyncio.run(main())
```
