# prefill_regressor.py
"""Static regression model for estimating prefill time from batch size and prompt length."""

import numpy as np
import asyncio
import aiohttp
import time
from transformers import AutoTokenizer
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler
from typing import List, Tuple, Dict, Optional
from collections import defaultdict # Added

# Assume request_generator.py is in the same directory or on PYTHONPATH.
from request_generator import generate_prompt_with_tokens, send_test_request

class PrefillTimeRegressor:
    """
    Static regressor.
    Workflow:
    1. Call trigger_warmup_requests to send load.
    2. Inject collected (batch_size, prompt_len, time) samples externally through add_data.
    3. Call fit() when the collected data volume reaches the expectation.
    """

    def __init__(self):
        self.model = LinearRegression(fit_intercept=True)
        self.scaler = StandardScaler()
        self._is_fitted = False
        self._coeffs = {'a': 0.0, 'b': 0.0, 'c': 0.0, 'd': 0.0}
        
        # Training data buffer.
        self._training_data: List[Tuple[int, int, float]] = []

    def _transform_features(self, batchsize: int, prompt_length: int) -> np.ndarray:
        return np.array([[batchsize * prompt_length, prompt_length, batchsize]])

    def fit(self):
        """Manually trigger fitting."""
        if not self._training_data:
            print("[Regressor] Warning: No data to fit.")
            return

        data = self._training_data
        
        # Print detailed data-collection statistics.
        print("\n--- 📊 Data Collection Summary ---")
        stats = defaultdict(list)
        for bs, pl, t in data:
            stats[(bs, pl)].append(t)
        
        # Sort output by batch size and then prompt length.
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

        # De-normalized coefficients.
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
        Data injection interface for external callers.
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
        Only sends requests to trigger load.
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
                # Print the configuration currently being triggered.
                print(f"   >> Firing: BatchSize={bs}, PromptLen={pl} (x{repeats_per_config})")

                for i in range(repeats_per_config):
                    # Regenerate the prompt each round to avoid repeatedly sending the exact same context and inflating cache hits.
                    prompts = [generate_prompt_with_tokens(tokenizer, pl) for _ in range(bs)]

                    # Send requests concurrently.
                    round_start_ts = time.perf_counter()
                    dispatch_ts = []
                    tasks = []
                    for p in prompts:
                        dispatch_ts.append(time.perf_counter())
                        tasks.append(
                            send_test_request(
                                session,
                                vllm_config["host"],
                                vllm_config["port"],
                                vllm_config["model_id"],
                                p,
                            )
                        )

                    # Record prompt dispatch intervals within the same round; intervals over 10ms may affect same-batch aggregation.
                    if len(dispatch_ts) > 1:
                        gaps_ms = [
                            (dispatch_ts[j] - dispatch_ts[j - 1]) * 1000
                            for j in range(1, len(dispatch_ts))
                        ]
                        max_gap_ms = max(gaps_ms)
                        if max_gap_ms > 10:
                            print(
                                f"[WARN] Dispatch gap too large: "
                                f"BS={bs}, PL={pl}, repeat={i+1}, max_gap={max_gap_ms:.2f}ms"
                            )
                    
                    results = await asyncio.gather(*tasks)
                    for task_idx, ttft in enumerate(results, start=1):
                        if ttft is None:
                            print(
                                f"[TTFT] BS={bs}, PL={pl}, repeat={i+1}, "
                                f"task={task_idx}/{bs}, ttft=FAILED"
                            )
                        else:
                            print(
                                f"[TTFT] BS={bs}, PL={pl}, repeat={i+1}, "
                                f"task={task_idx}/{bs}, ttft={ttft*1000:.2f}ms"
                            )

                    # Record each request TTFT in the same round and convert it to first-token arrival offset relative to round_start.
                    # Note: arrival_offsets mainly help observe pseudo-serialization; training samples default to the mean request TTFT.
                    valid_ttfts = []
                    arrival_offsets = []
                    for idx, ttft in enumerate(results):
                        if ttft is not None and ttft > 0:
                            valid_ttfts.append(ttft)
                            arrival_offsets.append((dispatch_ts[idx] - round_start_ts) + ttft)

                    if valid_ttfts:
                        ttft_min_ms = min(valid_ttfts) * 1000
                        ttft_avg_ms = (sum(valid_ttfts) / len(valid_ttfts)) * 1000
                        ttft_max_ms = max(valid_ttfts) * 1000
                        arrival_span_ms = (max(arrival_offsets) - min(arrival_offsets)) * 1000 if len(arrival_offsets) > 1 else 0.0
                        print(
                            f"[INFO] Repeat TTFT stats: BS={bs}, PL={pl}, repeat={i+1}, "
                            f"valid={len(valid_ttfts)}/{bs}, "
                            f"min/avg/max={ttft_min_ms:.1f}/{ttft_avg_ms:.1f}/{ttft_max_ms:.1f}ms, "
                            f"arrival_span={arrival_span_ms:.1f}ms"
                        )

                        # Support different sampling policies.
                        # The current experiment policy recommends mid_minmax:
                        # batch_time = (min_ttft + max_ttft) / 2
                        sample_policy = str(vllm_config.get("batch_sample_policy", "mid_minmax")).lower()
                        if sample_policy == "max_arrival":
                            sample_value = max(arrival_offsets)
                        elif sample_policy == "max_ttft":
                            sample_value = max(valid_ttfts)
                        elif sample_policy == "min_ttft":
                            sample_value = min(valid_ttfts)
                        elif sample_policy == "mid_minmax":
                            sample_value = (min(valid_ttfts) + max(valid_ttfts)) / 2
                        else:
                            sample_value = sum(valid_ttfts) / len(valid_ttfts)
                        self.add_data(bs, pl, sample_value)
                    else:
                        print(
                            f"[WARN] No valid TTFT collected in this repeat: "
                            f"BS={bs}, PL={pl}, repeat={i+1}"
                        )
                    
                    # Appropriate interval.
                    await asyncio.sleep(0.5) 
        
        print("\n--- 🏁 All Warmup Requests Sent ---")
        print("[INFO] Warmup TTFT data collected from request responses.")

    def predict(self, batchsize: int, prompt_length: int) -> float:
        if not self._is_fitted: return 0.0
        X = self._transform_features(batchsize, prompt_length)
        X_scaled = self.scaler.transform(X)
        pred = self.model.predict(X_scaled)[0]
        return max(0.001, pred)

    def get_coefficients(self) -> dict:
        return self._coeffs
