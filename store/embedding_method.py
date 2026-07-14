from .knowledge_interface import EmbeddingModel, VectorIndex
from typing import List, Sequence, Tuple
import math, random, hashlib


class DummyEmbeddingModel(EmbeddingModel):
    """
    Placeholder Embedding model: uses hash + random to simulate fixed-dimensional vectors.
    Only used for debugging the pipeline, not for real quality.
    Function: generate fixed-dimensional vectors with hash + random so each text embedding is reproducible.
    """
    def __init__(self, dim: int = 64):
        self.dim = dim

    def encode_vector(self, texts: List[str]) -> List[List[float]]:
        vectors: List[List[float]] = []
        for t in texts:
            # Use an md5 hash as the random seed so identical text produces identical vectors
            seed = int(hashlib.md5(t.encode()).hexdigest(), 16)
            random.seed(seed)
            vec = [random.random() for _ in range(self.dim)]
            vectors.append(vec)
        return vectors
# TODO: integrate an embedding model for text scheduling



class DummyVectorIndex(VectorIndex):
    """
        Simple in-memory vector-index implementation for development and debugging.
        Uses Lists to store embeddings and IDs, and retrieves by cosine similarity.
    """
    def __init__(self, dim: int):
        self._dim = dim
        self._embeddings: List[List[float]] = []    # Stores all embeddings
        self._ids: List[int] = []                   # knowledge_id values aligned one-to-one with embeddings

    def dim(self) -> int:
        return self._dim

    def _check_dim(self, vec: Sequence[float]) -> None:
        if len(vec) != self.dim:
            raise ValueError(f"embedding dim mismatch: expect {self._dim}, got {len(vec)}")

    @staticmethod
    def _cosine_similarity(vec1: Sequence[float], vec2: Sequence[float]) -> float:
        """Compute cosine similarity as the retrieval score."""
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
        """Add one embedding."""
        self._check_dim(embedding)
        self._embeddings.append(list(embedding))
        self._ids.append(knowledge_id)

    def search(self, query_embedding: Sequence[float], top_k: int) -> List[Tuple[int, float]]:
        """
            Brute-force search over all vectors (O(N)).
            Return top_k results.
        """
        self._check_dim(query_embedding)
        scores: List[Tuple[int, float]] = []
        for emb, kid in zip(self._embeddings, self._ids):
            sim = self._cosine_similarity(emb, query_embedding)
            scores.append((kid,sim))
        scores.sort(key=lambda x: x[1], reverse=True)   # Sort descending
        return scores[:top_k]



# TODO: implement a FAISS-based vector index and connect it to base.py afterward
class FaissVectorIndex(VectorIndex):
    """
    Reserved FAISS vector-index implementation. Later this can be implemented with faiss.IndexFlatL2 or similar.
    Currently only provides the interface skeleton and does not actively import faiss, avoiding environment errors.
    """

    def __init__(self, dim: int):
        self._dim = dim
        # TODO: Initialize your faiss.Index here, for example:
        # import faiss
        # self._index = faiss.IndexFlatL2(dim)
        # self._ids = []  # Maintain the ID list yourself or use IndexIDMap

    def dim(self) -> int:
        return self._dim

    def add_vector(self, embedding: Sequence[float], knowledge_id: int) -> None:
        # TODO: Convert embedding to numpy.float32 and then add it to the index
        # Record knowledge_id at the same time
        raise NotImplementedError("FaissVectorIndex.add is not implemented yet")

    def search(
        self,
        query_embedding: Sequence[float],
        top_k: int,
    ) -> List[Tuple[int, float]]:
        # TODO: Search with faiss and return a (knowledge_id, score) list
        raise NotImplementedError("FaissVectorIndex.search is not implemented yet")
