# prefill_predictor.py

import asyncio
import logging
import numpy as np
from typing import Optional, List, Tuple, Dict, Any

from prefill_regressor import PrefillTimeRegressor

# ==========================================
# 1. 配置区域 (保持不变)
# ==========================================
VLLM_CONFIG_DEFAULT = {
    "host": "0.0.0.0",
    "port": 8000,
    "model_id": "llama3-70b",
    "tokenizer_path": "/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct/"
    # 训练样本口径：
    # mid_minmax(默认) / mean_ttft / max_arrival / max_ttft / min_ttft
    "batch_sample_policy": "mid_minmax",
}

BATCH_SIZES_TO_TEST = range(1, 9)
TOKEN_LENGTHS_TO_TEST = range(64, 2048, 64)

WARM_UP_CONFIGS_DEFAULT = [
    (bs, pl)
    for bs in BATCH_SIZES_TO_TEST
    for pl in TOKEN_LENGTHS_TO_TEST
    if bs * pl <= 10000  # 过滤掉bs*pl大于10000的组合
]

# ==========================================
# 2. 单例管理 (保持不变)
# ==========================================
_regressor: Optional[PrefillTimeRegressor] = None
_lock = asyncio.Lock()

async def get_regressor() -> PrefillTimeRegressor:
    global _regressor
    async with _lock:
        if _regressor is None:
            print("[TTFT Predictor] Initializing Regressor...")
            _regressor = PrefillTimeRegressor()
            # 快速冷启动 Dummy Data
            dummy_data = [(1, 100, 0.04), (1, 1000, 0.22), (8, 100, 0.06), (8, 1000, 0.35), (4, 500, 0.12)]
            print("[TTFT Predictor] Seeding with dummy data for fast start...")
            try:
                for d in dummy_data: _regressor.add_data(*d)
                _regressor.fit()
            except Exception as e:
                logging.error(f"[TTFT Predictor] Dummy init failed: {e}")
    return _regressor

# ==========================================
# 3. 核心功能接口 (保持不变)
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
# 4. 真实预热逻辑 (大幅修改：逐个配置测试)
# ==========================================

async def perform_detailed_warmup(
    vllm_config: Dict = VLLM_CONFIG_DEFAULT,
    repeats: int = 3
):
    """
    [管理接口] 执行真实的基准测试流程。
    修改后：逐个配置触发，并实时打印该配置的测试结果。
    """
    print("\n" + "="*50)
    print("🚀 [Detailed Warmup] Starting Step-by-Step Benchmark...")
    print(f"   Target: {vllm_config['host']}:{vllm_config['port']}")
    print(f"   Batch Sample Policy: {vllm_config.get('batch_sample_policy', 'mid_minmax')}")
    print(f"   Total Configs: {len(WARM_UP_CONFIGS_DEFAULT)}")
    print("="*50)
    
    regressor = await get_regressor()

    # 1. 清空旧数据，准备重新收集
    regressor.clear_data()
    
    total_configs = len(WARM_UP_CONFIGS_DEFAULT)
    
    try:
        # 2. 遍历每个配置，逐个触发并等待
        for idx, (bs, pl) in enumerate(WARM_UP_CONFIGS_DEFAULT):
            print(f"\n[{idx+1}/{total_configs}] Testing: BatchSize={bs}, PromptLen={pl} ...", end="", flush=True)
            
            # 记录当前缓冲区已有的大小，用于计算本次新增了多少数据
            start_data_count = len(regressor._training_data)
            # trigger_warmup_requests 现在按“每个 repeat 一条批次样本”写入数据
            expected_new_points = repeats
            
            # 2.1 触发当前配置的请求
            # 我们构造一个只包含当前配置的单元素列表
            current_config = [(bs, pl)]
            await regressor.trigger_warmup_requests(
                test_configs=current_config,
                vllm_config=vllm_config,
                repeats_per_config=repeats
            )
            
            # 2.2 等待数据回流
            # 轮询检查数据量是否增加了预期数量
            max_retries = 30 # 最多等30秒
            collected_this_round = []
            
            while max_retries > 0:
                current_data_count = len(regressor._training_data)
                # 检查是否收集够了
                if current_data_count >= start_data_count + expected_new_points:
                    # 更新收集到的这批数据的batch_size和prompt_length为当前测试的参数
                    for i in range(start_data_count, current_data_count):
                        # 获取当前数据点
                        _, _, time_old = regressor._training_data[i]
                        # 更新为正确的batch_size和prompt_length
                        regressor._training_data[i] = (bs, pl, time_old)
                    break
                await asyncio.sleep(0.5)
                max_retries -= 1
            
            # 2.3 提取并打印本次测试结果
            # 从 buffer 末尾提取出最新加入的数据点
            new_data_points = regressor._training_data[start_data_count:]
            if new_data_points:
                times = [t for _, _, t in new_data_points]
                avg_time = np.mean(times)
                print(f" Done. Avg TTFT: {avg_time*1000:.2f} ms ({len(times)} samples)")
            else:
                print(" Timeout/Failed. No data collected.")

        # 3. 所有配置跑完，统一拟合
        final_count = len(regressor._training_data)
        if final_count == 0:
            print("\n❌ [Detailed Warmup] No data collected at all.")
            return

        print(f"\n[Detailed Warmup] All tests finished. Total points: {final_count}. Fitting model...")
        regressor.fit()
        
        # 打印最终系数
        coeffs = regressor.get_coefficients()
        print("\n✅ [Detailed Warmup] Model calibrated.")
        print(f"   a (B*L) = {coeffs['a']:.2e}")
        print(f"   b (L)   = {coeffs['b']:.2e}")
        print(f"   c (B)   = {coeffs['c']:.2e}")
        print(f"   d (Int) = {coeffs['d']:.3f}")
        print("="*50 + "\n")
        
    except Exception as e:
        logging.error(f"❌ [Detailed Warmup] Failed: {e}", exc_info=True)

# ... (main block 保持不变)
if __name__ == "__main__":
    # 配置日志
    logging.basicConfig(level=logging.INFO)
    async def main():
        # ... (同上)
        pass
    asyncio.run(main())
