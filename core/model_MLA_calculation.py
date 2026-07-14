"""FLOP estimators for MLA-style model prefill computation."""

# from CacheRoute.model import ModelConfig


class MLAmodel:
    """Estimate per-layer and full-prefill FLOPs for MLA model configs."""

    @classmethod
    def calc_mla_layer_flops(cls, m_cfg, task, cache_len=0) -> float:
        """
        Estimate TFLOPs required by one MLA layer for the task prefill.

        Args:
            m_cfg: MLA model configuration with dimensions and layer settings.
            task: Task-like object containing token_length and bs.
            cache_len: Previous context length, defaulting to 0.

        Returns:
            Estimated per-layer compute cost in TFLOPs.
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

        # Q down-projection plus up-projection.
        q_down_proj = 2 * bs * seq_len * hidden_dim * q_lora_rank
        q_up_proj = 2 * bs * seq_len * q_lora_rank * heads * (qk_head_dim + qk_rope_head_dim)
        q_linear = q_down_proj + q_up_proj

        # KV down-projection plus up-projection.
        kv_down_proj = 2 * bs * seq_len * hidden_dim * (kv_lora_rank + qk_rope_head_dim)
        kv_up_proj = 2 * bs * heads * context_length * kv_lora_rank * (qk_head_dim + v_head_dim)
        kv_linear = kv_down_proj + kv_up_proj

        # Attention score and value aggregation computation.
        causal_div = m_cfg.causal_mask_cof if getattr(m_cfg, "causal_mask_cof", 1) else 1
        kv_scores = (2 * bs * heads * seq_len * context_length * (qk_head_dim + qk_rope_head_dim)) // causal_div
        qkv = (2 * bs * heads * seq_len * context_length * v_head_dim) // causal_div
        attention = kv_scores + qkv

        # Output projection.
        output = 2 * bs * seq_len * n_heads * v_head_dim * hidden_dim

        layer_flops = (q_linear + kv_linear + attention + output) / 1e12   # TFLOPs

        return layer_flops

    @classmethod
    def calc_mla_prefill_flops(cls, m_cfg, task, cache_len=0) -> float:
        """
        Estimate total MLA prefill TFLOPs for all model layers.

        Args:
            m_cfg: MLA model configuration with model_layer.
            task: Task-like object containing sequence length information.
            cache_len: Previous context length, defaulting to 0.

        Returns:
            Estimated full-prefill compute cost in TFLOPs.
        """
        layer_flops = MLAmodel.calc_mla_layer_flops(m_cfg, task, cache_len)
        prefill_flops = layer_flops * m_cfg.model_layer     # Prefill = per-layer cost * layer count.

        return prefill_flops
