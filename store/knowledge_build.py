"""
将raw_data通过Embedding模型转化为Knowledge_base.yaml
"""
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional
from core.config import DEFAULT_EMBED_MODEL
import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))
KNOWLEDGE_YAML_PATH = ROOT_DIR / "data"

from model import EmbeddingEngine

def build_knowledge_yaml(
    source_path: str,
    output_path: str,
    model_name: Optional[str] = None,
) -> None:
    """
        从原始文本知识库 YAML 生成带向量的 knowledge_base.yaml

        source_path: 原始文本知识库 (data/raw_data.yaml)
        output_path: 输出的向量知识库 (data/knowledge_base.yaml)
        model_name : 可选，显式指定 embedding 模型名
    """
    src_file = Path(source_path)
    out_file = Path(output_path)

    if not src_file.exists():
        raise FileNotFoundError(f"source yaml not found: {src_file}")

    with src_file.open("r", encoding="utf-8") as f:
        config: Dict[str, Any] = yaml.safe_load(f) or {}

    embedder = EmbeddingEngine(
        model_name=model_name or DEFAULT_EMBED_MODEL
    )
    dim = embedder.dim

    default_servers: List[str] = list(config.get("default_servers", []) or [])
    raw_items: List[Dict[str, Any]] = list(config.get("knowledge_items", []) or [])

    out_items: List[Dict[str, Any]] = []
    next_id = 1

    for item in raw_items:
        # id：有则用之，无则自动递增
        if "id" in item:
            kid = int(item["id"])
        else:
            kid = next_id
            next_id += 1

        content: str = (item.get("content") or "").strip()
        if not content:
            # 空内容直接跳过
            continue

        # length：优先用用户给的，否则 fallback 为字符长度
        length = int(item.get("length", len(content)))

        llm_systems = list(item.get("llm_systems", []) or [])
        kdn_servers = list(item.get("kdn_servers", []) or [])

        # 生成向量
        vec = embedder.encode_vector([content])[0]  # (dim,)
        vec_list = [float(x) for x in vec.tolist()]

        # text 摘要：优先用 item.text，否则取前 10 个字
        summary = (item.get("text") or content[:10]).strip()

        out_items.append(
            {
                "id": kid,
                "length": length,
                "embedding": vec_list,
                "text": summary,
                "llm_systems": llm_systems,
                "kdn_servers": kdn_servers,
            }
        )

    out_config = {
        "knowledge_dim": dim,
        "default_servers": default_servers,
        "knowledge_items": out_items,
    }

    with out_file.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            out_config,
            f,
            sort_keys=False,
            allow_unicode=True,
            width=120,  # 行不要太窄
        )

    print(f"[build] embedding dim = {dim}, items = {len(out_items)}")
    print(f"[build] written to: {out_file.resolve()}")


if __name__ == "__main__":
    # 按需修改这两个路径
    build_knowledge_yaml(
        source_path= str(KNOWLEDGE_YAML_PATH / "raw_data.yaml"),
        output_path=str(KNOWLEDGE_YAML_PATH / "knowledge_base.yaml"),
        model_name="intfloat/multilingual-e5-large-instruct",
    )