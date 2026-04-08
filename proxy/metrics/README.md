### 队列预测器

在`ttft_benchmark_table.json`中写入数据后，执行`python3 ttft_four_term_regressor.py`即可完成预测模型回归并将参数自动写入`ttft_coefficient.json`中。

简单验证归回有效性：
```
python3 queue_predictor.py --length 2880 --bs 5 --ms
```
