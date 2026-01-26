from typing import List, Sequence, Tuple
from abc import ABC, abstractmethod

"""
    定义EmbeddingModel:：embedding方法的抽象规范
    定义VectorIndex：矢量检索的抽象方法规范
    具体实现在embedding_index_method.py
"""


class EmbeddingModel(ABC):
    """
        抽象的 Embedding 模型接口。
        后续可以用 OpenAI / 自建模型 / 本地 HF 模型等来实现。
        硬性要求：返回固定维度的向量（dim）
    """

    @abstractmethod
    def encode_vector(self, texts: List[str]) -> List[List[float]]:
        """
            输入：texts: List[str]  —— 待编码的文本列表
            List[List[float]]：每个文本对应一个 embedding 向量
        """
        raise NotImplementedError



class VectorIndex(ABC):
    """
        抽象的向量索引接口。
        当前可以用内存实现，后续可以替换为 FAISS（速度更快）等。
    """

    @abstractmethod
    def dim(self) -> int:
        """返回索引向量维度（所有 embedding 必须统一维度）。"""
        raise NotImplementedError

    @abstractmethod
    def add_vector(self, embedding: Sequence[float], knowledge_id: int) -> None:
        """
        向索引中新增一条向量，对应一个 knowledge_id。
        """
        raise NotImplementedError

    @abstractmethod
    def search(self, query_embedding: Sequence[float], top_k: int) -> List[Tuple[int, float]]:
        """
        基于 query_embedding 搜索相似度最高的向量。
        返回：[(knowledge_id, score), ...] 按 score 降序。
        """
        raise NotImplementedError