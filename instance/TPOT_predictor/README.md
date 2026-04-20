# TPOT Predictor

`TPOT_predictor` 用于采集并输出按真实 `sequence_length` 组织的 TPOT 曲线，支持区间接口、异常值剔除、四项线性拟合和 decode 预测。

---

## 1. 新区间接口（核心）

新增：

```python
collect_tpot_range(
    batch_sizes: List[int],
    length_start: int,
    length_end: int,
    ...
)
```

这里的 `length_start/length_end` 语义是：**真实 sequence_length 区间 [a,b]**。

接口会自动：

1. 把区间映射成内部待测 `target_prompt_length` 配置；
2. 采集数据后输出 `sequence_length=a..b` 的连续曲线；
3. 对缺失点标记来源（`observed / interpolated / fitted / none`）。

---

## 2. 真实长度与最小可观测长度

定义：

- `sequence_length = real_input_length + token_index - 1`
- `real_input_length` 来自 `apply_chat_template(..., tokenize=True, add_generation_prompt=True)`

因为 chat template 有固定 offset，最小可观测长度通常不会从 1 开始。
输出里会包含：

- `min_observed_sequence_length`
- `max_observed_sequence_length`

---

## 3. 异常值剔除（可配置）

按 `(batch_size, sequence_length)` 分桶后做 robust filtering：

- `outlier_method`: `"mad" | "iqr" | "none"`
- `outlier_threshold`
- `min_samples_for_filter`

默认：`mad + threshold=3.5 + min_samples=5`。

样本太少时不激进过滤（仅保守处理）。

每个长度点同时保留：

- `raw_samples`
- `filtered_samples`
- `raw_mean_tpot_ms`
- `filtered_mean_tpot_ms`
- `filtered_median_tpot_ms`
- `filtered_p95_tpot_ms`
- `outlier_count`
- `value_source`

默认用于拟合与 decode 的值：`filtered_mean_tpot_ms`。

---

## 4. 导出

`export_lengthwise_curve(output_path)` 支持：

- `.csv`
- `.json`
- `.xlsx`（若无 `openpyxl`，自动回退 CSV）

字段：

- `batch_size`
- `sequence_length`
- `raw_samples`
- `filtered_samples`
- `raw_mean_tpot_ms`
- `filtered_mean_tpot_ms`
- `filtered_median_tpot_ms`
- `filtered_p95_tpot_ms`
- `outlier_count`
- `value_source`

---

## 5. summarize 区间查看

新增：

```python
summarize_results(summary, full_curve_bs=1, length_range=(a,b))
```

可只看某个 bs 且某个长度区间内的完整曲线。

---

## 6. 最小调用样例

```python
import asyncio
from tpot_predictor import collect_tpot_range, summarize_results

async def main():
    result = await collect_tpot_range(
        batch_sizes=[1, 2, 4],
        length_start=128,
        length_end=192,
        max_tokens=16,
        repeats=3,
        outlier_method="mad",
        outlier_threshold=3.5,
        min_samples_for_filter=5,
        fit_after_collect=True,
    )

    summary = result["summary"]
    print(summarize_results(summary, full_curve_bs=1, length_range=(128, 192)))

    reg = result["regressor"]
    reg.export_lengthwise_curve("instance/TPOT_predictor/output/range_128_192_bs124.xlsx", rows=result["range_curve"])

asyncio.run(main())
```

---

## 7. 四项模型与 decode

拟合模型：

\[
TPOT(bs,length)=a1\cdot bs\cdot length+a2\cdot bs+a3\cdot length+a4
\]

decode 计算：

\[
T_{decode}(l,b,bs)=\sum_{i=0}^{b-1}TPOT(bs,l+i)
\]

缺失点处理优先级：`observed -> interpolated -> fitted -> none`。
