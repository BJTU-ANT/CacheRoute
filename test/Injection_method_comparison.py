import numpy as np
import matplotlib.pyplot as plt
from CacheRoute.model import model_config
from CacheRoute.core import Prompt
from CacheRoute.core import MLAmodel





if __name__ == "__main__":
    device_flops = 419
    device_type = "RTX5090"
    network_bw = 0.250
    mfu = 0.5
    cof = 0.7

    # Read model-parameter demo
    task = Prompt(model="DeepseekV3", model_type="MLA", token_length=100, bs=1)
    model_config = model_config.get_config_by_model(task.model)