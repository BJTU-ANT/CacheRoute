import numpy as np
import matplotlib.pyplot as plt
import time
from CacheRoute.model import model_config
from CacheRoute.core import Prompt
from CacheRoute.core import MLAmodel
from CacheRoute.core import TokenizerRegistry


"""
    Input a user question to test automatic tokenization and model-info extraction capability.
    Input: Class Prompt
    Output: full class prompt, loaded model information, and estimated compute time.
"""

if __name__ == "__main__":
    device_flops = 419
    device_type = "RTX5090"
    network_bw = 0.250
    mfu = 0.5
    cof = 0.7

    # Scheduler tokenizer warmup
    TokenizerRegistry.warmup_tokenizers("DeepseekV3")

    # Extract task information and compute sequence length with the tokenizer
    start = time.perf_counter()
    task = Prompt.extract_prompt_info(
        model="DeepseekV3",
        user_prompt="There is an apple; it is large, round, and juicy.",
    )
    end = time.perf_counter()
    print(task)
    time = (end - start) * 1000
    print(f"extract_task_info elapsed:{time:.4f} ms")



    # Read task model parameters
    model_config = model_config.get_config_by_model(task.model)
    print(model_config)

    # Compute-cost estimate
    layer_flops = MLAmodel.calc_mla_layer_flops(model_config, task)
    print(f"Layer Computation of {task.model} for {task.token_length} length question is: {layer_flops} TFLOPS ")

    prefill_flops = MLAmodel.calc_mla_prefill_flops(model_config, task)
    print(f"Prefill Computation of {task.model} for {task.token_length} length question is: {prefill_flops} TFLOPS ")

