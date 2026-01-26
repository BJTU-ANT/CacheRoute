from .knowledge_interface import EmbeddingModel, VectorIndex
from typing import List, Sequence, Tuple
import math, random, hashlib


class DummyEmbeddingModel(EmbeddingModel):
    """
    占位 Embedding 模型：用 hash + random 模拟固定维度向量。
    仅用于调试流水线，不用于真实效果。
    功能：用 hash + 随机数生成固定维度向量，确保每个文本的 embedding 可复现。
    """
    def __init__(self, dim: int = 64):
        self.dim = dim

    def encode_vector(self, texts: List[str]) -> List[List[float]]:
        vectors: List[List[float]] = []
        for t in texts:
            # 用 md5 hash 作为随机种子，使相同文本生成相同向量
            seed = int(hashlib.md5(t.encode()).hexdigest(), 16)
            random.seed(seed)
            vec = [random.random() for _ in range(self.dim)]
            vectors.append(vec)
        return vectors
# TODO:集成embedding模型进行文本调度



class DummyVectorIndex(VectorIndex):
    """
        简单的内存向量索引实现，用于开发调试。
        底层用 List 存 embedding 和 id，通过余弦相似度检索。
    """
    def __init__(self, dim: int):
        self._dim = dim
        self._embeddings: List[List[float]] = []    # 存储所有 embedding
        self._ids: List[int] = []                   # 与 embedding 一一对应的 knowledge_id

    def dim(self) -> int:
        return self._dim

    def _check_dim(self, vec: Sequence[float]) -> None:
        if len(vec) != self.dim:
            raise ValueError(f"embedding dim mismatch: expect {self._dim}, got {len(vec)}")

    @staticmethod
    def _cosine_similarity(vec1: Sequence[float], vec2: Sequence[float]) -> float:
        """计算余弦相似度，作为检索得分"""
        dot = 0.0
        n_vec1 = 0.0
        n_vec2 = 0.0
        for v1, v2 in zip(vec1, vec2):
            dot += v1 * v2
            n_vec1 += v1 * v1
            n_vec2 += v2 * v2
        if n_vec1 == 0.0 or n_vec2 == 0.0:
            return 0.0
        return dot / math.sqrt(n_vec1 * n_vec2)

    def add_vector(self, embedding: Sequence[float], knowledge_id: int) -> None:
        """新增一条 embedding"""
        self._check_dim(embedding)
        self._embeddings.append(list(embedding))
        self._ids.append(knowledge_id)

    def search(self, query_embedding: Sequence[float], top_k: int) -> List[Tuple[int, float]]:
        """
            brute-force 检索所有向量（O(N)）
            返回 top_k 结果。
        """
        self._check_dim(query_embedding)
        scores: List[Tuple[int, float]] = []
        for emb, kid in zip(self._embeddings, self._ids):
            sim = self._cosine_similarity(emb, query_embedding)
            scores.append((kid,sim))
        scores.sort(key=lambda x: x[1], reverse=True)   # 降序排序
        return scores[:top_k]



# TODO: 实现基于FAISS的向量索引，实现后拼到base.py
class FaissVectorIndex(VectorIndex):
    """
    预留的 FAISS 向量索引实现。后续你可以用 faiss.IndexFlatL2 等来实现。
    当前仅给出接口骨架，不主动 import faiss，避免环境报错。
    """

    def __init__(self, dim: int):
        self._dim = dim
        # TODO: 在这里初始化你的 faiss.Index，例如：
        # import faiss
        # self._index = faiss.IndexFlatL2(dim)
        # self._ids = []  # 自己维护 id 列表，或用 IndexIDMap

    def dim(self) -> int:
        return self._dim

    def add_vector(self, embedding: Sequence[float], knowledge_id: int) -> None:
        # TODO: 将 embedding 转成 numpy.float32，然后 add 到 index
        # 同时记录 knowledge_id
        raise NotImplementedError("FaissVectorIndex.add 尚未实现")

    def search(
        self,
        query_embedding: Sequence[float],
        top_k: int,
    ) -> List[Tuple[int, float]]:
        # TODO: 用 faiss 搜索，返回 (knowledge_id, score) 列表
        raise NotImplementedError("FaissVectorIndex.search 尚未实现")
