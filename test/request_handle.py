import time
from CacheRoute.model import model_config
from CacheRoute.core import Request
from CacheRoute.core import TokenizerRegistry


"""
    Input a user question to test the ability to build the request structure.
    Input: user question
    Output: full class request
"""

if __name__ == "__main__":
    # Scheduler tokenizer warmup
    TokenizerRegistry.warmup_tokenizers("DeepseekV3")

    # Extract task information and compute sequence length with the tokenizer
    start = time.perf_counter()
    raw_data = {
        "model": "DeepseekV3",
        "user_prompt": "Generate a high-performance scheduling strategy based on the requirements below and explain the key steps."
    }
    request = Request.build_request(raw_data,"192.168.0.167")
    end = time.perf_counter()
    print(request)
    time = (end - start) * 1000
    print(f"build_request_info elapsed:{time:.4f} ms")

    # Read task model parameters
    # model_config = model_config.get_config_by_model(request.Prompt.model)
    # print(model_config)