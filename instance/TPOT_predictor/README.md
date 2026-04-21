# TPOT Predictor

`TPOT_predictor` 用于采集并输出按真实 `sequence_length` 组织的 TPOT 曲线，支持区间接口、异常值剔除、平滑诊断、四项线性拟合和 decode 预测。

---

## 1. TPOT 时间戳定义

当前 TPOT 捕获是基于**客户端流式接收时间戳**：

- 在 `send_stream_request_for_tpot(...)` 里使用 `time.perf_counter()` 记录 token 事件到达时间。
- 第一个生成 token 的时间差记为 TTFT。
- 后续 token 的时间差记为 TPOT step delta。

这意味着 TPOT 包含了：服务端 decode + 流式刷出节奏 + 网络传输抖动 + 客户端事件循环调度抖动。

---

## 2. 为什么会出现偶发尖峰

流式测量中尖峰常见来源：

1. 多 token 被同一 SSE event 聚合后一起到达；
2. 网络短时抖动导致 chunk 堆积后突发到达；
3. 客户端调度抖动（event loop 被其他任务占用）；
4. 某些 `(bs, sequence_length)` 桶样本太少，单点异常难以被桶内统计抵消。

---

## 3. 新增稳健处理（本次重点）

### 3.1 默认统计口径

默认用于拟合/预测的列从 `filtered_mean_tpot_ms` 切换为更稳健的：

- `filtered_median_tpot_ms`（优先）

并保留均值列作为参考。

### 3.2 平滑列

每个 bs 内，按 sequence_length 顺序新增滑动中位数平滑：

- `smoothed_tpot_ms`

平滑不会覆盖原始值，只是额外诊断列。

### 3.3 双层异常保护

每个点新增：

- `is_low_confidence`：`filtered_samples < min_samples_for_filter`
- `suspicious_spike`：低置信点且相对邻域出现突增

### 3.4 default_tpot_ms 规则

每个点新增：

- `default_tpot_ms`

优先级：

1. `filtered_median_tpot_ms`
2. `smoothed_tpot_ms`
3. `filtered_mean_tpot_ms`

---

## 4. 新区间接口（核心）

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
2. 输出 `sequence_length=a..b` 的连续曲线；
3. 对每个点标记来源：`observed / interpolated / fitted / none`。

---

## 5. 导出字段（csv/json/xlsx）

导出列：

- `batch_size`
- `sequence_length`
- `raw_samples`
- `filtered_samples`
- `raw_mean_tpot_ms`
- `filtered_mean_tpot_ms`
- `filtered_median_tpot_ms`
- `filtered_p95_tpot_ms`
- `outlier_count`
- `is_low_confidence`
- `suspicious_spike`
- `smoothed_tpot_ms`
- `default_tpot_ms`
- `value_source`

支持 `.xlsx`，若环境无 `openpyxl` 自动回退 `.csv`。

---

## 6. summarize 区间查看

```python
summarize_results(summary, full_curve_bs=1, length_range=(a,b))
```

会打印：

- `raw_samples / filtered_samples / outlier_count`
- `is_low_confidence / suspicious_spike`
- `filtered_median_tpot_ms / smoothed_tpot_ms / default_tpot_ms`

---

## 7. 最小调用样例

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
        smooth_window=5,
        spike_ratio_threshold=1.8,
        fit_after_collect=True,
    )

    summary = result["summary"]
    print(summarize_results(summary, full_curve_bs=1, length_range=(128, 192)))

    reg = result["regressor"]
    reg.export_lengthwise_curve(
        "instance/TPOT_predictor/output/range_128_192_bs124.xlsx",
        rows=result["range_curve"],
    )

asyncio.run(main())
```
