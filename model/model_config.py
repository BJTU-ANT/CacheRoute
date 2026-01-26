# 读取模型参数信息

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


# 自动定位到当前脚本的目录，以获取配置文件
PROJECT_ROOT  = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "model" / f"model_configs.yaml"


@dataclass(frozen=True)
class ModelConfig:
    # 定义模型结构体，包含模型的相关参数
    model_type: str                 # 模型类型
    model_layer: int                # 模型层数
    heads: int                      # 模型注意力头数
    qk_head_dim: int                # 每个头的QK向量维度
    v_head_dim: int                 # 每个头的V向量维度，通常与QK相同
    q_lora_rank: int                # Q的LoRA低秩维度
    kv_lora_rank: int               # KV的LoRA低秩维度
    hidden_dim: int                 # 隐藏层维度
    qk_rope_head_dim: int           # 每个头QK向量的RoPE位置维度，即前64维会加入旋转位置编码信息，用于建模序列位置
    n_heads: int
    causal_mask_cof: int = 2        # 开启因果=2，关闭=1
    n_shared_experts: int = 1       # MoE专有，共享专家数量
    n_routed_experts: int = 256     # MoE专有，专家门控可动态选择的专家数量
    moe_inter_dim: int = 2048       # MoE专有，专家层中间维度
    n_activated_experts: int = 8    # MoE专有，每个token实际激活的专家数

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModelConfig":
        # 允许配置里缺失 n_heads 时，回退成 heads
        if "n_heads" not in d and "heads" in d:
            d = {**d, "n_heads": d["heads"]}
        # 基本校验（可按需加更多）
        required = [
            "model_layer","heads","qk_head_dim","kv_lora_rank","hidden_dim","q_lora_rank",
            "qk_rope_head_dim","v_head_dim","n_heads"
        ]
        missing = [k for k in required if k not in d]
        if missing:
            raise ValueError(f"ModelConfig 缺少必需字段: {missing}")
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
    """读取 YAML/JSON，返回 {model_name: {param: value}} 的映射。"""
    p = pathlib.Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到配置文件: {p}")

    if p.suffix.lower() in {".yaml", ".yml"}:
        if not _YAML_OK:
            raise RuntimeError("未安装 pyyaml，无法读取 YAML。请 `pip install pyyaml`")
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    elif p.suffix.lower() == ".json":
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        raise ValueError(f"不支持的配置格式: {p.suffix}，仅支持 .yaml/.yml/.json")

    if not isinstance(data, dict):
        raise ValueError("配置文件顶层必须是字典：{模型名: 参数字典}")
    return data


def _normalize_model_key(model_name: str) -> str:
    """
    将外部传入的 model_name（可能是路径/别名/文件名）规范化为内部逻辑 key。

    规则：
      1) 如果是路径，取最后一段（basename）；
      2) 去掉常见权重文件后缀（.bin / .pt / .safetensors）；
      3) 通过别名表把各种写法统一到 YAML 中配置的标准 key；
      4) 返回规范化后的“候选 key”（还需要在 get_config_by_model 中和 mapping 实际对照）。
    """
    s = str(model_name).strip()

    # 1) 路径 → basename
    if "/" in s or "\\" in s:
        s = pathlib.Path(s).name  # e.g. DeepSeek-R1-Distill-Qwen-1.5B 或 deepseekv3.bin

    # 2) 去掉常见权重后缀
    for suf in (".bin", ".pt", ".safetensors"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break

    lower = s.lower()

    # 3) 别名映射表：key 全部小写
    ALIASES = {
        # 这里可以按你的 YAML 配置来写“标准名”
        # 思路：左边是各种乱写法（统一小写），右边是 YAML 里真正的 key
        "deepseekv3": "DeepseekV3",
        "deepseek-v3": "DeepseekV3",
        "deepseek_r1_distill_qwen_1.5b": "DeepSeek-R1-Distill-Qwen-1.5B",
        "deepseek-r1-distill-qwen-1.5b": "DeepSeek-R1-Distill-Qwen-1.5B",
        # 以后有别的模型，直接在这里加
    }

    if lower in ALIASES:
        return ALIASES[lower]

    # 默认直接返回 basename 版本，后续再和 YAML keys 做一次大小写无关匹配
    return s


def get_config_by_model(model_name: str,
                            config_path: Union[str, pathlib.Path] = DEFAULT_CONFIG_PATH
                            ) -> ModelConfig:
    """
    根据模型名读取外部文件，返回对应的 ModelConfig。

    支持的 model_name 形式：
      - 逻辑名：DeepseekV3
      - 逻辑名变种：deepseekv3 / DeepSeek-R1-Distill-Qwen-1.5B 等
      - 路径：/workspace/.../DeepSeek-R1-Distill-Qwen-1.5B
      - 文件名：DeepseekV3.bin / DeepSeek-R1-Distill-Qwen-1.5B.safetensors

    内部会自动：
      1) 规范化 model_name → 逻辑 key；
      2) 先尝试精确匹配 mapping[key]；
      3) 再用大小写无关的方式兜底。
    """
    mapping = _load_mapping(config_path)

    # 第一步：规范化（处理路径/后缀/别名）
    key_candidate = _normalize_model_key(model_name)

    # 第二步：直接精确匹配（比如 YAML 里本来就写了 DeepSeek-R1-Distill-Qwen-1.5B）
    if key_candidate in mapping:
        key = key_candidate
    else:
        # 第三步：大小写无关匹配（再兜一层底）
        normalized = {k.lower(): k for k in mapping.keys()}
        cand_lower = key_candidate.lower()
        if cand_lower in normalized:
            key = normalized[cand_lower]
        else:
            # 再退一步：用原始 model_name 也尝试一次大小写无关匹配（防止 normalize 反而搞丢）
            raw = str(model_name)
            raw_lower = raw.lower()
            if raw in mapping:
                key = raw
            elif raw_lower in normalized:
                key = normalized[raw_lower]
            else:
                raise KeyError(
                    f"配置中不存在模型: model_name={model_name}, "
                    f"规范化后候选 key={key_candidate}；可用项：{list(mapping.keys())}"
                )

    return ModelConfig.from_dict(mapping[key])


