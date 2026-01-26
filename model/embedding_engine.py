# embedding_engine.py
from typing import List, Union

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

from core.config import DEFAULT_EMBED_MODEL

from store import EmbeddingModel


class EmbeddingEngine(EmbeddingModel):
    """
    本地 Embedding 模型封装：
      - 启动时加载一次模型到 GPU/CPU
      - 提供 encode_vector(texts) -> np.ndarray
      - 暴露 dim 方便知识库KnowledgeTable 使用
    """
    def __init__(self, model_name: str) -> None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._device = device

        # 打开 cudnn benchmark，卷积类模型会快一些（非必须，但一般有益）
        if device == "cuda":
            torch.backends.cudnn.benchmark = True

        self._model = SentenceTransformer(model_name, device=device)
        self._model.eval()
        torch.set_grad_enabled(False)
        # 供知识库维度检查用
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
        return vecs  # (N, dim) 的 numpy 数组

    def prewarm_scheduler_pipeline(self, scheduler, logger=None) -> None:
        """
            在调度器启动阶段调用。
            作用：模拟一次完整的 build_request 流程，触发：
                - tokenizer
                - embedding encode
                - knowledge_retriever
                - FAISS search
            等所有冷启动逻辑。

            参数：
                scheduler: FastAPI app 对象（你现在的 scheduler）
                logger: 可选 logger，用于打印预热耗时
        """
        import time
        from core import Request as SchedulerRequest  # 避免循环 import

        # 从 scheduler.state 拿知识库
        knowledge_table = getattr(scheduler.state, "knowledge_table", None)
        if knowledge_table is None:
            if logger:
                logger.warning(
                    "[Prewarm] skip prewarm_scheduler_pipeline: knowledge_table is None"
                )
            return

        # 构造一条“假”的用户请求（chat/completions 风格）
        dummy_payload = {
            "model": "dummy-model",
            "messages": [
                {
                    "role": "user",
                    "content": "这是一次用于预热的调度测试请求，用来触发 embedding 和知识检索。",
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