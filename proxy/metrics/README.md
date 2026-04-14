### 队列预测器

队列预测器预测proxy将任务送入instance队列，到收到第一个token回复的时间。它具体包含Prefill时间和队列等待时间。

在`ttft_benchmark_table.json`中写入数据后，执行`python3 ttft_four_term_regressor.py`即可完成预测模型回归并将参数自动写入`ttft_coefficient.json`中。

简单验证归回有效性：
```
python3 queue_predictor.py --length 2880 --bs 5 --ms
```

### Redis拉取时间回归

当你有类似 `kvcache_size_gb, redis_pull_ms_1..N` 的实验表时，可执行（支持 CSV/JSON）：
```
python3 redis_pull_regressor.py --data-file /path/to/redis_pull_table.csv
```
会在 `proxy/metrics/redis_pull_coefficients.json` 写入线性系数（ms）：
`redis_pull_ms = a * kvcache_size_gb + b`

JSON 输入格式示例：
```json
{
  "rows": [
    {
      "name": "q92",
      "actual_hit_length_tokens": 768,
      "kvcache_size_gb": 0.0292608,
      "redis_pull_ms": [129.814, 125.814, 135.814, 122.814, 132.814, 109.814, 111.814, 117.814]
    },
    {
      "name": "q81",
      "actual_hit_length_tokens": 256,
      "kvcache_size_gb": 0.0097536,
      "redis_pull_ms_1": 110.095,
      "redis_pull_ms_2": 76.095,
      "redis_pull_ms_3": 94.095
    }
  ]
}
```
也支持以下 JSON 结构：
- 顶层是数组：`[ {...}, {...} ]`
- 顶层是对象且样本键是 `rows` / `data` / `samples`。

如果样本里没有 `kvcache_size_gb`，但有 `actual_hit_length_tokens`，可加：
```
python3 redis_pull_regressor.py --data-file /path/to/redis_pull_table.json --kv-gb-per-token 0.0000381
```
此时会按 `kvcache_size_gb = actual_hit_length_tokens * kv_gb_per_token` 自动换算后拟合。

在预测器侧可直接调用：
```
python3 queue_predictor.py --length 2880 --bs 1 --ms --kvcache-size-gb 0.048768
```

统一口径预测（推荐）：
```
python3 queue_predictor.py \
  --length 2880 \
  --bs 1 \
  --knowledge-length 1330 \
  --align-size 256 \
  --kv-gb-per-token 0.0000381
```
会结构化输出两类场景：
- `text-based`：基于 `--length`（即 total_length）四项式估算的纯计算时间。
- `kvcache-based`（提供 `--knowledge-length` 时）：知识命中长度（256 对齐）、KVCache 大小、剩余待计算长度、剩余文本计算时间、Redis 拉取时间、以及拉取+剩余重计算总时间。
