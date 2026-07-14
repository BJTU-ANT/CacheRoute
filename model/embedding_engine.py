"""Local sentence-transformer embedding engine and scheduler prewarm helper."""
# embedding_engine.py
from typing import List, Union

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

from core.config import DEFAULT_EMBED_MODEL

from store import EmbeddingModel


class EmbeddingEngine(EmbeddingModel):
    """
    Local embedding model wrapper:
      - Loads the model once onto GPU/CPU during startup.
      - Provides encode_vector(texts) -> np.ndarray.
      - Exposes dim for KnowledgeTable dimension checks.
    """
    def __init__(self, model_name: str) -> None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._device = device

        # Enable cudnn benchmark; convolution-style models may run faster. Optional but usually helpful.
        if device == "cuda":
            torch.backends.cudnn.benchmark = True

        self._model = SentenceTransformer(model_name, device=device)
        self._model.eval()
        torch.set_grad_enabled(False)
        # Used for knowledge-base dimension checks.
        self.dim = int(self._model.get_sentence_embedding_dimension())

    @property
    def device(self):
        return self._device

    def encode_vector(self, texts: Union[str, List[str]]) -> np.ndarray:
        if isinstance(texts, str):
            texts = [texts]
        vecs = self._model.encode(
            texts,
            batch_size=32,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vecs  # numpy array with shape (N, dim)

    def prewarm_scheduler_pipeline(self, scheduler, logger=None) -> None:
        """
            Call during scheduler startup.
            Purpose: simulate one complete build_request flow to trigger:
                - tokenizer
                - embedding encode
                - knowledge_retriever
                - FAISS search
            and other cold-start logic.

            Args:
                scheduler: FastAPI app object for the current scheduler.
                logger: optional logger used to print prewarm latency.
        """
        import time
        from core import Request as SchedulerRequest  # Avoid circular imports.

        # Read the knowledge table from scheduler.state.
        knowledge_table = getattr(scheduler.state, "knowledge_table", None)
        if knowledge_table is None:
            if logger:
                logger.warning(
                    "[Prewarm] skip prewarm_scheduler_pipeline: knowledge_table is None"
                )
            return

        # Build a dummy user request in chat/completions style.
        dummy_payload = {
            "model": "dummy-model",
            "messages": [
                {
                    "role": "user",
                    "content": "This is a scheduler prewarm test request used to trigger embedding and knowledge retrieval.",
                }
            ],
            "max_tokens": 16,
        }

        t0 = time.perf_counter()
        try:
            req_obj = SchedulerRequest.build_request(
                url_path="/v1/chat/completions",
                payload=dummy_payload,
                user_addr="127.0.0.1",
                request_id=0,
                embedder=self,
                knowledge_table=knowledge_table,
            )
            t1 = time.perf_counter()
            if logger:
                logger.info(
                    "[Prewarm] build_request pipeline ok, "
                    "Knowledge_List=%s, Knowledge_length=%s, elapsed=%.1f ms",
                    getattr(req_obj.Service, "Knowledge_List", None),
                    getattr(req_obj.Service, "Knowledge_length", None),
                    (t1 - t0) * 1000,
                )
        except Exception as e:
            t1 = time.perf_counter()
            if logger:
                logger.warning(
                    "[Prewarm] build_request pipeline failed: %s, elapsed=%.1f ms",
                    e,
                    (t1 - t0) * 1000,
                )