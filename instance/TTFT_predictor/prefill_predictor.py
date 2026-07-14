# prefill_predictor.py
"""Singleton-style TTFT predictor facade with detailed warmup support."""

import asyncio
import logging
import numpy as np
from typing import Optional, List, Tuple, Dict, Any

from prefill_regressor import PrefillTimeRegressor

# ==========================================
# 1. Configuration area (unchanged).
# ==========================================
VLLM_CONFIG_DEFAULT = {
    "host": "0.0.0.0",
    "port": 8000,
    "model_id": "llama3-70b",
    "tokenizer_path": "/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct/",
    # Training sample policy:
    # mid_minmax(default) / mean_ttft / max_arrival / max_ttft / min_ttft
    "batch_sample_policy": "mid_minmax",
}

BATCH_SIZES_TO_TEST = range(1, 9)
TOKEN_LENGTHS_TO_TEST = range(64, 2048, 64)

WARM_UP_CONFIGS_DEFAULT = [
    (bs, pl)
    for bs in BATCH_SIZES_TO_TEST
    for pl in TOKEN_LENGTHS_TO_TEST
    if bs * pl <= 10000  # Filter out combinations where bs*pl is greater than 10000.
]

# ==========================================
# 2. Singleton management (unchanged).
# ==========================================
_regressor: Optional[PrefillTimeRegressor] = None
_lock = asyncio.Lock()

async def get_regressor() -> PrefillTimeRegressor:
    global _regressor
    async with _lock:
        if _regressor is None:
            print("[TTFT Predictor] Initializing Regressor...")
            _regressor = PrefillTimeRegressor()
            # Fast cold-start dummy data.
            dummy_data = [(1, 100, 0.04), (1, 1000, 0.22), (8, 100, 0.06), (8, 1000, 0.35), (4, 500, 0.12)]
            print("[TTFT Predictor] Seeding with dummy data for fast start...")
            try:
                for d in dummy_data: _regressor.add_data(*d)
                _regressor.fit()
            except Exception as e:
                logging.error(f"[TTFT Predictor] Dummy init failed: {e}")
    return _regressor

# ==========================================
# 3. Core functional interface (unchanged).
# ==========================================
async def predict_ttft(batch_size: int, prompt_length: int) -> float:
    regressor = await get_regressor()
    if not regressor._is_fitted:
        return 0.02 * batch_size + 0.0002 * prompt_length
    return max(0.001, regressor.predict(batch_size, prompt_length))

async def update_prefill_data(batch_size: int, prompt_length: int, prefill_time: float):
    regressor = await get_regressor()
    if prefill_time < 0.001 or prefill_time > 60.0:
        return
    try:
        regressor.add_data(batch_size, prompt_length, prefill_time)
    except Exception as e:
        logging.error(f"[TTFT Predictor] Data collection failed: {e}")

# ==========================================
# 4. Real warmup logic (major change: test each configuration individually).
# ==========================================

async def perform_detailed_warmup(
    vllm_config: Dict = VLLM_CONFIG_DEFAULT,
    repeats: int = 3
):
    """
    [Management interface] Execute the real benchmark flow.
    After modification: trigger each configuration individually and print its result in real time.
    """
    print("\n" + "="*50)
    print("🚀 [Detailed Warmup] Starting Step-by-Step Benchmark...")
    print(f"   Target: {vllm_config['host']}:{vllm_config['port']}")
    print(f"   Batch Sample Policy: {vllm_config.get('batch_sample_policy', 'mid_minmax')}")
    print(f"   Total Configs: {len(WARM_UP_CONFIGS_DEFAULT)}")
    print("="*50)
    
    regressor = await get_regressor()

    # 1. Clear old data and prepare to recollect.
    regressor.clear_data()
    
    total_configs = len(WARM_UP_CONFIGS_DEFAULT)
    
    try:
        # 2. Iterate over configurations, triggering and waiting one by one.
        for idx, (bs, pl) in enumerate(WARM_UP_CONFIGS_DEFAULT):
            print(f"\n[{idx+1}/{total_configs}] Testing: BatchSize={bs}, PromptLen={pl} ...", end="", flush=True)
            
            # Record current buffer size to calculate how many data points this run adds.
            start_data_count = len(regressor._training_data)
            # trigger_warmup_requests now writes data as one batch sample per repeat.
            expected_new_points = repeats
            
            # 2.1 Trigger requests for the current configuration.
            # Build a single-element list containing only the current configuration.
            current_config = [(bs, pl)]
            await regressor.trigger_warmup_requests(
                test_configs=current_config,
                vllm_config=vllm_config,
                repeats_per_config=repeats
            )
            
            # 2.2 Wait for data to return.
            # Poll until the data volume has increased by the expected amount.
            max_retries = 30 # Wait up to 30 seconds.
            collected_this_round = []
            
            while max_retries > 0:
                current_data_count = len(regressor._training_data)
                # Check whether enough data has been collected.
                if current_data_count >= start_data_count + expected_new_points:
                    # Update the collected batch_size and prompt_length to the current test parameters.
                    for i in range(start_data_count, current_data_count):
                        # Get the current data point.
                        _, _, time_old = regressor._training_data[i]
                        # Update to the correct batch_size and prompt_length.
                        regressor._training_data[i] = (bs, pl, time_old)
                    break
                await asyncio.sleep(0.5)
                max_retries -= 1
            
            # 2.3 Extract and print this test result.
            # Extract the newest data points from the end of the buffer.
            new_data_points = regressor._training_data[start_data_count:]
            if new_data_points:
                times = [t for _, _, t in new_data_points]
                avg_time = np.mean(times)
                print(f" Done. Avg TTFT: {avg_time*1000:.2f} ms ({len(times)} samples)")
            else:
                print(" Timeout/Failed. No data collected.")

        # 3. Fit once after all configurations finish.
        final_count = len(regressor._training_data)
        if final_count == 0:
            print("\n❌ [Detailed Warmup] No data collected at all.")
            return

        print(f"\n[Detailed Warmup] All tests finished. Total points: {final_count}. Fitting model...")
        regressor.fit()
        
        # Print final coefficients.
        coeffs = regressor.get_coefficients()
        print("\n✅ [Detailed Warmup] Model calibrated.")
        print(f"   a (B*L) = {coeffs['a']:.2e}")
        print(f"   b (L)   = {coeffs['b']:.2e}")
        print(f"   c (B)   = {coeffs['c']:.2e}")
        print(f"   d (Int) = {coeffs['d']:.3f}")
        print("="*50 + "\n")
        
    except Exception as e:
        logging.error(f"❌ [Detailed Warmup] Failed: {e}", exc_info=True)

# ... Main block remains unchanged. ...
if __name__ == "__main__":
    # Configure logging.
    logging.basicConfig(level=logging.INFO)
    async def main():
        # ... Same as above. ...
        pass
    asyncio.run(main())
