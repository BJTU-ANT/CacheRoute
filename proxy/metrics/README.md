### 队列预测器

队列预测器预测proxy将任务送入instance队列，到收到第一个token回复的时间。它具体包含Prefill时间和队列等待时间。

在`ttft_benchmark_table.json`中写入数据后，执行`python3 ttft_four_term_regressor.py`即可完成预测模型回归并将参数自动写入`ttft_coefficient.json`中。

简单验证归回有效性：
```
python3 queue_predictor.py --length 2880 --bs 5 --ms
```
