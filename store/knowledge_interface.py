from typing import List, Sequence, Tuple
from abc import ABC, abstractmethod

"""
    Defines EmbeddingModel: the abstract contract for embedding methods.
    Defines VectorIndex: the abstract contract for vector retrieval methods.
    Concrete implementations are in embedding_index_method.py.
"""


class EmbeddingModel(ABC):
    """
        Abstract Embedding model interface.
        Later this can be implemented with OpenAI, self-hosted models, local HF models, etc.
        Hard requirement: return fixed-dimensional vectors (dim).
    """

    @abstractmethod
    def encode_vector(self, texts: List[str]) -> List[List[float]]:
        """
            Input: texts: List[str] -- text list to encode.
            List[List[float]]:One embedding vector per text.
        """
        raise NotImplementedError



class VectorIndex(ABC):
    """
        Abstract vector-index interface.
        It can currently use an in-memory implementation and later be replaced by FAISS or similar for speed.
    """

    @abstractmethod
    def dim(self) -> int:
        """Return the index vector dimension; all embeddings must have the same dimension."""
        raise NotImplementedError

    @abstractmethod
    def add_vector(self, embedding: Sequence[float], knowledge_id: int) -> None:
        """
        Add one vector to the index for a corresponding knowledge_id.
        """
        raise NotImplementedError

    @abstractmethod
    def search(self, query_embedding: Sequence[float], top_k: int) -> List[Tuple[int, float]]:
        """
        Search for the most similar vectors based on query_embedding.
        Returns:[(knowledge_id, score), ...] sorted by descending score.
        """
        raise NotImplementedError