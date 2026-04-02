# prefill_regressor.py

import numpy as np
import asyncio
import aiohttp
from transformers import AutoTokenizer
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler
from typing import List, Tuple, Dict, Optional
from collections import defaultdict # 新增

# 假设 request_generator.py 在同一目录下或在 Python 路径中
from request_generator import generate_prompt_with_tokens, send_test_request

class PrefillTimeRegressor:
    """
    静态回归器。
    流程：
    1. 调用 trigger_warmup_requests 发送负载。
    2. 外部通过 add_data 注入收集到的 (batch_size, prompt_len, time)。
    3. 当收集到的数据量达到预期时，调用 fit() 进行拟合。
    """

    def __init__(self):
        self.model = LinearRegression(fit_intercept=True)
        self.scaler = StandardScaler()
        self._is_fitted = False
        self._coeffs = {'a': 0.0, 'b': 0.0, 'c': 0.0, 'd': 0.0}
        
        # 训练数据缓冲区
        self._training_data: List[Tuple[int, int, float]] = []

    def _transform_features(self, batchsize: int, prompt_length: int) -> np.ndarray:
        return np.array([[batchsize * prompt_length, prompt_length, batchsize]])

    def fit(self):
        """手动触发拟合"""
        if not self._training_data:
            print("[Regressor] Warning: No data to fit.")
            return

        data = self._training_data
        
        # === 新增：打印详细的数据收集统计 ===
        print("\n--- 📊 Data Collection Summary ---")
        stats = defaultdict(list)
        for bs, pl, t in data:
            stats[(bs, pl)].append(t)
        
        # 按 BS 然后 PL 排序输出
        for (bs, pl), times in sorted(stats.items()):
            avg_t = np.mean(times)
            min_t = np.min(times)
            max_t = np.max(times)
            count = len(times)
            print(f"   Config (BS={bs}, PL={pl}): Count={count}, Avg={avg_t*1000:.2f}ms (Min={min_t*1000:.1f}, Max={max_t*1000:.1f})")
        print("----------------------------------\n")
        # ======================================

        X_raw = np.array([self._transform_features(bs, pl)[0] for bs, pl, ttft in data])
        y = np.array([ttft for bs, pl, ttft in data])

        print(f"[Regressor] Fitting model with {len(y)} samples...")

        self.scaler.fit(X_raw)
        X_scaled = self.scaler.transform(X_raw)
        self.model.fit(X_scaled, y)
        self._is_fitted = True

        # 反归一化系数
        raw_coeffs_scaled = self.model.coef_
        intercept_scaled = self.model.intercept_
        mean = self.scaler.mean_
        scale = self.scaler.scale_
        scale[scale == 0] = 1

        raw_coeffs = raw_coeffs_scaled / scale
        intercept = intercept_scaled - np.sum(raw_coeffs * mean)

        self._coeffs = {
            'a': raw_coeffs[0],
            'b': raw_coeffs[1],
            'c': raw_coeffs[2],
            'd': intercept
        }

        print("✅ Model fitted successfully.")
        print(f"   Coeffs: a={self._coeffs['a']:.2e}, b={self._coeffs['b']:.2e}, c={self._coeffs['c']:.2e}, d={self._coeffs['d']:.3f}")

    def add_data(self, batch_size: int, prompt_length: int, prefill_time: float):
        """
        供外部调用的数据注入接口。
        """
        self._training_data.append((batch_size, prompt_length, prefill_time))

    def clear_data(self):
        self._training_data = []

    async def trigger_warmup_requests(
        self,
        test_configs: List[Tuple[int, int]],
        vllm_config: Dict,
        repeats_per_config: int = 3
    ):
        """
        只负责发送请求以触发负载。
        """
        print("--- 🚀 Triggering Warmup Requests ---")
        print(f"[INFO] Configs: {len(test_configs)}, Repeats: {repeats_per_config}")
        
        try: 
            tokenizer = AutoTokenizer.from_pretrained(vllm_config["tokenizer_path"])
        except Exception as e: 
            print(f"[ERROR] Tokenizer load failed: {e}")
            return
        
        timeout = aiohttp.ClientTimeout(total=600)
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for bs, pl in test_configs:
                # 打印当前正在触发的配置
                print(f"   >> Firing: BatchSize={bs}, PromptLen={pl} (x{repeats_per_config})")
                
                for i in range(repeats_per_config):

                    prompts = [generate_prompt_with_tokens(tokenizer, pl) for _ in range(bs)]
                    
                    # 并发发送请求
                    tasks = [
                        send_test_request(
                            session, 
                            vllm_config["host"], 
                            vllm_config["port"], 
                            vllm_config["model_id"], 
                            p
                        ) for p in prompts
                    ]
                    
                    await asyncio.gather(*tasks)
                    
                    # 适当的间隔
                    await asyncio.sleep(0.5) 
        
        print("\n--- 🏁 All Warmup Requests Sent ---")
        print("[INFO] Waiting for data collection via 'update_prefill_data'...")

    def predict(self, batchsize: int, prompt_length: int) -> float:
        if not self._is_fitted: return 0.0
        X = self._transform_features(batchsize, prompt_length)
        X_scaled = self.scaler.transform(X)
        pred = self.model.predict(X_scaled)[0]
        return max(0.001, pred)

    def get_coefficients(self) -> dict:
        return self._coeffs
