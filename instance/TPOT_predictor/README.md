# TPOT Predictor

`TPOT_predictor` 参考 `TTFT_predictor` 结构实现，但目标是输出 **固定 batch size 下，随真实 sequence length 变化的 TPOT 曲线**。

---

## 1. 目录结构

- `tpot_predictor.py`：对外异步入口，负责触发 benchmark 与摘要输出。
- `tpot_regressor.py`：核心采集与聚合（当前偏统计，不做回归拟合）。
- `request_generator.py`：prompt 构造 + SSE 流式解析 + token 级时间记录。
- `__init__.py`：导出常用接口。

---

## 2. 关键口径

### 2.1 `target_prompt_length` 与 `real_input_length`

请求走 `/v1/chat/completions` 时，服务端会套 chat template，所以模型真实 prefill 长度不等于裸 prompt token 数。

实现中会同时记录：

- `target_prompt_length`：你想要的裸 prompt 目标长度。
- `real_input_length`：通过
  `tokenizer.apply_chat_template([{"role":"user","content":prompt}], tokenize=True, add_generation_prompt=True)`
  算出的真实输入长度。
- `input_length_offset = real_input_length - target_prompt_length`。

每个配置都会打印 debug：

- `target_prompt_length`
- `real_input_length`
- `diff`

用于定位你观测到的固定偏差（例如 -32/+32 是否由 template 导致）。

### 2.2 token 抓取口径（SSE 解析）

不再按 `iter_any()` 的 chunk 数当 token 数。

现在逻辑：

1. 解析 SSE `data:` 行并反序列化 JSON。
2. 只读取 `choices[0].delta.content` 的新增文本。
3. 把新增文本累积后重新 tokenize。
4. 根据“新增 token 数”展开记录（即使一个 event 带来多个 token，也会展开多个 step）。

### 2.3 sequence length 定义

对每个生成 step：

- `token_index` 从 1 开始（第一个生成 token）。
- `sequence_length = real_input_length + token_index - 1`。

即 `sequence_length` 表示“生成该 token 前，KV 序列当前长度”。

---

## 3. 输出组织

### 3.1 保留原始 records

每条任务记录包含：

- `batch_size`
- `target_prompt_length`
- `real_input_length`
- `input_length_offset`
- `token_steps`（含 `token_index`, `sequence_length`, `tpot_seconds`）

### 3.2 新增 length-wise TPOT 曲线（按 bs）

`summary.length_wise_by_bs` 的结构：

```json
{
  "batch_size": 4,
  "length_tpot_curve": [
    {
      "sequence_length": 1024,
      "samples": 12,
      "mean_tpot_ms": 8.2,
      "median_tpot_ms": 8.0,
      "p95_tpot_ms": 9.4
    }
  ]
}
```

可直接用于：

\[
T_{decode}(l, b) = \sum_{i=0}^{b-1} TPOT(bs, l+i)
\]

---

## 4. 使用

### 4.1 直接运行

```bash
cd instance/TPOT_predictor
python tpot_predictor.py
```

### 4.2 作为模块调用

```python
import asyncio
from tpot_predictor import collect_tpot_matrix

async def main():
    reg = await collect_tpot_matrix(
        configs=[(1, 256), (4, 512)],
        max_tokens=32,
        repeats=3,
    )
    reg.export_json("instance/TPOT_predictor/output/my_tpot.json")

asyncio.run(main())
```

---

## 5. 拟合建议（四项式）

拿到 `length_wise_by_bs` 后，可按每个 `bs` 拟合：

- 输入：`x = sequence_length`
- 标签：`y = mean_tpot_ms`（也可用 median）
- 模型：`y = a0 + a1*x + a2*x^2 + a3*x^3 + a4*x^4`

然后用拟合函数近似每个长度点，再做离散求和来估计 `T_decode(l,b)`。
