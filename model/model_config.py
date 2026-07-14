"""Model configuration loading and normalization helpers."""
# Read model parameter information.

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union
import json
import pathlib

try:
    import yaml  # pip install pyyaml
    _YAML_OK = True
except ImportError:
    _YAML_OK = False
    yaml = None


# Locate the current script directory automatically to find the config file.
PROJECT_ROOT  = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "model" / f"model_configs.yaml"


@dataclass(frozen=True)
class ModelConfig:
    # Define the model structure with relevant model parameters.
    model_type: str                 # Model type
    model_layer: int                # Number of model layers
    heads: int                      # Number of attention heads
    qk_head_dim: int                # QK vector dimension per head
    v_head_dim: int                 # V vector dimension per head, usually the same as QK
    q_lora_rank: int                # LoRA low-rank dimension for Q
    kv_lora_rank: int               # LoRA low-rank dimension for KV
    hidden_dim: int                 # Hidden-layer dimension
    qk_rope_head_dim: int           # RoPE positional dimension for each QK head; the first 64 dimensions include rotary position encoding for sequence positions
    n_heads: int
    causal_mask_cof: int = 2        # causal enabled=2, disabled=1
    n_shared_experts: int = 1       # MoE only: number of shared experts
    n_routed_experts: int = 256     # MoE only: number of dynamically routed experts
    moe_inter_dim: int = 2048       # MoE only: expert-layer intermediate dimension
    n_activated_experts: int = 8    # MoE only: number of experts activated per token

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModelConfig":
        # Fall back to heads when n_heads is absent from the config.
        if "n_heads" not in d and "heads" in d:
            d = {**d, "n_heads": d["heads"]}
        # Basic validation; add more checks as needed.
        required = [
            "model_layer","heads","qk_head_dim","kv_lora_rank","hidden_dim","q_lora_rank",
            "qk_rope_head_dim","v_head_dim","n_heads"
        ]
        missing = [k for k in required if k not in d]
        if missing:
            raise ValueError(f"ModelConfig missing required fields: {missing}")
        return cls(**d)

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()

    def __repr__(self):
        return (
            f"ModelConfig(\n"
            f"  model_type={self.model_type},\n"
            f"  heads={self.heads},\n"
            f"  qk_head_dim={self.qk_head_dim},\n"
            f"  kv_lora_rank={self.kv_lora_rank},\n"
            f"  hidden_dim={self.hidden_dim},\n"
            f"  q_lora_rank={self.q_lora_rank},\n"
            f"  qk_rope_head_dim={self.qk_rope_head_dim},\n"
            f"  v_head_dim={self.v_head_dim},\n"
            f"  n_heads={self.n_heads},\n"
            f"  causal_mask_cof={self.causal_mask_cof},\n"
            f"  n_shared_experts={self.n_shared_experts},\n"
            f"  n_routed_experts={self.n_routed_experts},\n"
            f"  moe_inter_dim={self.moe_inter_dim},\n"
            f"  n_activated_experts={self.n_activated_experts}\n"
            f")"
        )


def _load_mapping(path: Union[str, pathlib.Path]) -> Dict[str, Dict[str, Any]]:
    """Read YAML/JSON and return a {model_name: {param: value}} mapping."""
    p = pathlib.Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")

    if p.suffix.lower() in {".yaml", ".yml"}:
        if not _YAML_OK:
            raise RuntimeError("pyyaml is not installed, so YAML cannot be read. Run `pip install pyyaml`")
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    elif p.suffix.lower() == ".json":
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        raise ValueError(f"Unsupported config format: {p.suffix}, only .yaml/.yml/.json are supported")

    if not isinstance(data, dict):
        raise ValueError("Config file top level must be a dictionary: {model_name: parameter_dict}")
    return data


def _normalize_model_key(model_name: str) -> str:
    """
    Normalize external model_name values, which may be paths, aliases, or filenames, into internal logical keys.

    Rules:
      1) If it is a path, use the final segment (basename).
      2) Remove common weight-file suffixes (.bin / .pt / .safetensors).
      3) Use the alias table to map variants to canonical keys configured in YAML.
      4) Return the normalized candidate key, which get_config_by_model still checks against the actual mapping.
    """
    s = str(model_name).strip()

    # 1) Path -> basename.
    if "/" in s or "\\" in s:
        s = pathlib.Path(s).name  # e.g. DeepSeek-R1-Distill-Qwen-1.5B or deepseekv3.bin

    # 2) Remove common weight-file suffixes.
    for suf in (".bin", ".pt", ".safetensors"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break

    lower = s.lower()

    # 3) Alias table: all keys are lowercase.
    ALIASES = {
        # Add canonical names according to the YAML config.
        # Left side: lowercase variants; right side: the real YAML key.
        "deepseekv3": "DeepseekV3",
        "deepseek-v3": "DeepseekV3",
        "deepseek_r1_distill_qwen_1.5b": "DeepSeek-R1-Distill-Qwen-1.5B",
        "deepseek-r1-distill-qwen-1.5b": "DeepSeek-R1-Distill-Qwen-1.5B",
        # Add future models here directly.
    }

    if lower in ALIASES:
        return ALIASES[lower]

    # By default, return the basename and later match YAML keys case-insensitively.
    return s


def get_config_by_model(model_name: str,
                            config_path: Union[str, pathlib.Path] = DEFAULT_CONFIG_PATH
                            ) -> ModelConfig:
    """
    Read an external config file by model name and return its ModelConfig.

    Supported model_name forms:
      - Logical name: DeepseekV3
      - Logical-name variants: deepseekv3 / DeepSeek-R1-Distill-Qwen-1.5B, etc.
      - Path: /workspace/.../DeepSeek-R1-Distill-Qwen-1.5B
      - Filename: DeepseekV3.bin / DeepSeek-R1-Distill-Qwen-1.5B.safetensors

    Internally:
      1) Normalize model_name -> logical key.
      2) Try exact mapping[key] match first.
      3) Fall back to case-insensitive matching.
    """
    mapping = _load_mapping(config_path)

    # Step 1: normalize paths, suffixes, and aliases.
    key_candidate = _normalize_model_key(model_name)

    # Step 2: exact match, for example when YAML already contains DeepSeek-R1-Distill-Qwen-1.5B.
    if key_candidate in mapping:
        key = key_candidate
    else:
        # Step 3: case-insensitive fallback.
        normalized = {k.lower(): k for k in mapping.keys()}
        cand_lower = key_candidate.lower()
        if cand_lower in normalized:
            key = normalized[cand_lower]
        else:
            # Final fallback: try case-insensitive matching with raw model_name in case normalization lost information.
            raw = str(model_name)
            raw_lower = raw.lower()
            if raw in mapping:
                key = raw
            elif raw_lower in normalized:
                key = normalized[raw_lower]
            else:
                raise KeyError(
                    f"Model not found in config: model_name={model_name}, "
                    f"normalized candidate key={key_candidate}; available keys={list(mapping.keys())}"
                )

    return ModelConfig.from_dict(mapping[key])


