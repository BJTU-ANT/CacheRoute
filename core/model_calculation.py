# from CacheRoute.model import ModelConfig


class MLAmodel:

    @classmethod
    def calc_mla_layer_flops(cls, m_cfg, task, cache_len=0) -> float:
        """
        输入任务预计算长度，计算MLA模型单层推理所需要的flops计算量
            model_config<MLA的模型参数>
            task<任务信息，包含序列长度>
            cache_len<历史会话长度，默认0>
            输入：模型参数，任务长度
            输出：模型对该任务每一层的计算量
        """
        heads = m_cfg.heads
        n_heads = m_cfg.n_heads
        hidden_dim = m_cfg.hidden_dim
        qk_head_dim = m_cfg.qk_head_dim
        qk_rope_head_dim = m_cfg.qk_rope_head_dim
        v_head_dim = m_cfg.v_head_dim
        q_lora_rank = m_cfg.q_lora_rank
        kv_lora_rank = m_cfg.kv_lora_rank
        seq_len = task.token_length
        bs = task.bs
        context_length = seq_len + cache_len

        # Q的下采样+上采样
        q_down_proj = 2 * bs * seq_len * hidden_dim * q_lora_rank
        q_up_proj = 2 * bs * seq_len * q_lora_rank * heads * (qk_head_dim + qk_rope_head_dim)
        q_linear = q_down_proj + q_up_proj

        # KV的下采样+上采样
        kv_down_proj = 2 * bs * seq_len * hidden_dim * (kv_lora_rank + qk_rope_head_dim)
        kv_up_proj = 2 * bs * heads * context_length * kv_lora_rank * (qk_head_dim + v_head_dim)
        kv_linear = kv_down_proj + kv_up_proj

        # Attention计算
        causal_div = m_cfg.causal_mask_cof if getattr(m_cfg, "causal_mask_cof", 1) else 1
        kv_scores = (2 * bs * heads * seq_len * context_length * (qk_head_dim + qk_rope_head_dim)) // causal_div
        qkv = (2 * bs * heads * seq_len * context_length * v_head_dim) // causal_div
        attention = kv_scores + qkv

        # 输出线性层
        output = 2 * bs * seq_len * n_heads * v_head_dim * hidden_dim

        layer_flops = (q_linear + kv_linear + attention + output) / 1e12   # TFLOPS

        return layer_flops


    @classmethod
    def calc_mla_prefill_flops(cls, m_cfg, task, cache_len=0) -> float:
        """
                输入任务预计算长度，计算MLA模型整体Prefill所需要的flops计算量
                    model_config<MLA的模型参数>
                    task<任务信息，包含序列长度>
                    cache_len<历史会话长度，默认0>
                    输入：模型参数，任务长度
                    输出：模型对该任务的Prefill计算量
                """
        layer_flops = MLAmodel.calc_mla_layer_flops(m_cfg, task, cache_len)
        prefill_flops = layer_flops * m_cfg.model_layer     # Prefill = 单层计算量 * 层数

        return prefill_flops





