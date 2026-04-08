"""
store/knowledge_base.py
=======================
定义知识库维护单元数据结构
定义知识库单元维护基本方法

调度器启动知识库入口，负责：
  1) 从 YAML 文件中读取初始知识条目，构建 KnowledgeTable；
  2) 提供统一的知识表更新接口 apply_knowledge_update，方便后续接入：
       - HTTP API / gRPC / 消息队列等上层协议；
       - 调度器内部模块直接调用。

依赖：
  - base.py 中定义的：
      * KnowledgeTable
  - embedding_index_method.py 中定义的：
      * EmbeddingModel（具体实现，例如 DummyEmbeddingModel）
  - pyyaml 用于解析 YAML 配置文件
"""
import numpy as np
import math

from dataclasses import dataclass, field
from typing import Any, Dict, Sequence, List, Optional, Tuple
from util import parse_host_port
from store import EmbeddingModel

try:
    import yaml
except ImportError as e:
    raise ImportError("需要安装 pyyaml 库以支持 YAML 配置解析：pip install pyyaml") from e

try:
    import faiss
except ImportError:
    faiss = None


@dataclass
class KnowledgeUnit:
    """
        表征单个知识块的信息（不包含知识正文）：
        Class KnowledgeUnit
          |- embedding: 表征知识块的高维向量List[float]，用于向量检索来定位知识块信息
          |- length: 知识块长度（token数、字符数等，自定义）
          |- avail_llm_systems: 可用 LLM 系统地址列表,e.g., ["llm://10.0.0.3:8000"]
          |- avail_kdn_servers: 可用 KDN 服务器地址列表，e.g., ["kdn://10.0.1.5:9000"]
          |- text_abstract: （可选）知识的文本描述/摘要，仅用于展示和调试
          |- default_servers: 对所有知识单元可用的“默认服务器”（由 KnowledgeTable 注入）
    """
    embedding: List[float]
    length: int
    avail_llm_systems: List[str] = field(default_factory=list)
    avail_kdn_servers: List[str] = field(default_factory=list)
    text_abstract: Optional[str] = None
    default_servers: List[str] = field(default_factory=list)

    # --- KV cache related metadata (from KDN snapshot) ---
    kv_ready: int = 0
    kv_rel_dir: Optional[str] = None
    kv_dumped_keys: Optional[int] = None
    kv_updated_at: Optional[int] = None
    # 可选：保存完整正文，供 scheduler 按目标模型 tokenizer 估 token 长度。
    full_content: Optional[str] = None


class KnowledgeTable:
    """
    知识库表格：
      - 维护 knowledge_id -> KnowledgeUnit 的映射
      - 提供更新接口（增减某知识的 LLM / KDN）
      - 动态更新knowledge_id
      - 提供基于 embedding 的相似度检索接口

    注意：
      这里用的是最简单的“内存 + 余弦相似度”实现。
      后续你可以替换为 FAISS，只要保持 search_by_embedding 的接口不变即可。
    """

    def __init__(self, dim: int, default_servers: List[str] = None):
        self.dim = dim
        # 外部：kid(str) -> KnowledgeUnit
        self._units: Dict[str, KnowledgeUnit] = {}
        self._next_id = 0

        # 全局默认的文本注入服务器，对所有知识单元都可用
        self._default_servers : List[str] =list(default_servers or [])

        # 内部：FAISS 需要 int64 id，所以做 kid <-> int64 映射（对外透明）
        self._kid_to_i64: Dict[str, int] = {}
        self._i64_to_kid: Dict[int, str] = {}

        # ---- 新增：FAISS 索引相关 ----
        self._faiss_index = None
        self._faiss_ids: List[int] = []  # row_idx -> knowledge_id 映射


    # ----------------------------------------------
    # ------------------- 基础工具 ------------------
    # ----------------------------------------------
    def _check_dim(self, vec: Sequence[float]):
        """检查embedding维度是否正确"""
        if len(vec) != self.dim:
            raise ValueError(f"Embedding dim mismatch: expect {self.dim}, got {len(vec)}")

    @staticmethod
    def _cosine_similarity(vec1: Sequence[float], vec2: Sequence[float]) -> float:
        """计算两个向量间的余弦相似度，作为检索得分"""
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

    def assign_new_id(self) -> int:
        """为“新来的知识”（没有显式指定 ID）分配一个递增 ID。"""
        kid = self._next_id
        self._next_id += 1
        return kid

    def _update_next_id_with_existing(self, knowledge_id: int) -> None:
        """
            当外部显式指定了 knowledge_id（例如从 YAML 中读到 id=5），
            需要保证 _next_id 始终大于现有最大 ID。
        """
        if knowledge_id >= self._next_id:
            self._next_id = knowledge_id + 1

    def get_llm_parsed(self, knowledge_id: int, default_port: int = 7000):
        """解析指定知识ID的字符串，返回 [(ip, port), (ip, port), ...]"""
        unit = self._units.get(knowledge_id)
        return [parse_host_port(addr, default_port) for addr in unit.avail_llm_systems]

    def get_kdn_parsed(self, knowledge_id: int, default_port: int = 8000):
        """解析指定知识ID的字符串，返回 [(ip, port), (ip, port), ...]"""
        unit = self._units.get(knowledge_id)
        return [parse_host_port(addr, default_port) for addr in unit.avail_kdn_servers]

    @staticmethod
    def _kid_to_int64(kid: str) -> int:
        """
        Map 64-hex kid -> non-negative int64 for FAISS.
        Note:
        - We only need stability, not readability.
        - Use 64-bit slice + clear sign bit to keep it non-negative.
        """
        kid = kid.strip().lower()
        x = int(kid[:16], 16)  # first 64 bits
        return x & 0x7fffffffffffffff

    def _register_kid(self, kid: str) -> int:
        """
        冲突回退逻辑，防止重复的int64碰撞
        """
        kid = kid.strip().lower()
        i64 = self._kid_to_int64(kid)

        if i64 in self._i64_to_kid and self._i64_to_kid[i64] != kid:
            # fallback to last 64 bits
            i64 = (int(kid[-16:], 16) & 0x7fffffffffffffff)
            if i64 in self._i64_to_kid and self._i64_to_kid[i64] != kid:
                raise RuntimeError(f"kid->int64 collision: {kid} vs {self._i64_to_kid[i64]}")

        self._kid_to_i64[kid] = i64
        self._i64_to_kid[i64] = kid
        return i64

    def clone_without_index(self) -> "KnowledgeTable":
        """
        Clone KnowledgeTable structure WITHOUT FAISS index.
        Used for atomic-swap refresh.
        """
        new_table = KnowledgeTable(dim=self.dim, default_servers=list(self._default_servers))

        # 1) deep copy units
        from copy import deepcopy
        new_table._units = deepcopy(self._units)
        new_table._next_id = self._next_id

        # 2) copy kid<->int64 mapping
        new_table._kid_to_i64 = dict(self._kid_to_i64)
        new_table._i64_to_kid = dict(self._i64_to_kid)

        # 3) FAISS index intentionally NOT copied
        new_table._faiss_index = None

        return new_table

    # --------------------------------------------------
    # ------------------- 知识管理接口 -------------------
    # --------------------------------------------------
    def upsert_kid(self, kid: str, unit: KnowledgeUnit) -> None:
        """
        upsert_knowledge是早期基于yaml本地文件的知识库维护方式，新版本下应只用本方法
        :param kid:
        :param unit:
        :return:
        """
        kid = (kid or "").strip().lower()
        if not kid:
            raise ValueError("empty kid")
        self._check_dim(unit.embedding)

        self._register_kid(kid)
        self._units[kid] = unit

    def delete_kids(self, kids: list[str]) -> None:
        """
        Delete knowledge units by kid.
        """
        for kid in kids:
            kid = kid.strip().lower()
            if kid not in self._units:
                continue

            i64 = self._kid_to_i64.pop(kid, None)
            if i64 is not None:
                self._i64_to_kid.pop(i64, None)

            self._units.pop(kid, None)

        # FAISS index must be rebuilt by caller


    def upsert_knowledge(
            self,
            knowledge_id: int,
            embedding: Sequence[float],
            length: int,
            avail_llm_systems: Optional[List[str]] = None,
            avail_kdn_servers: Optional[List[str]] = None,
            text_abstract: Optional[str] = None,
    ) -> None:
        """
            新增或更新一个知识块。
            如果 knowledge_id 已存在，则覆盖原有记录；
            如果为新 ID，则插入并更新 _next_id。
            调度器或后台管理程序可以调用这个接口动态维护知识库。
        """
        self._check_dim(embedding)

        unit = KnowledgeUnit(
            embedding=list(embedding),
            length=int(length),
            avail_llm_systems=list(avail_llm_systems or []),
            avail_kdn_servers=list(avail_kdn_servers or []),
            text_abstract=text_abstract,
            # 每个知识单元自动继承当前全局默认服务器列表
            default_servers=list(self._default_servers),
        )
        self._units[knowledge_id] = unit
        # 保证 next_id 始终大于最大已使用 ID
        self._update_next_id_with_existing(knowledge_id)

    def get_unit(self, knowledge_id: int) -> KnowledgeUnit:
        """获取某知识ID的单元信息"""
        return self._units[knowledge_id]

    def add_llm_for_knowledge(self, knowledge_id: int, llm_addr: str) -> None:
        """更新知识的可用LLM系统"""
        unit = self.get_unit(knowledge_id)
        if llm_addr not in unit.avail_llm_systems:
            unit.avail_llm_systems.append(llm_addr)

    def remove_llm_for_knowledge(self, knowledge_id: int, llm_addr: str) -> None:
        """移除知识的可用LLM系统"""
        unit = self.get_unit(knowledge_id)
        unit.avail_llm_systems = [x for x in unit.avail_llm_systems if x != llm_addr]

    def add_kdn_for_knowledge(self, knowledge_id: int, kdn_addr: str) -> None:
        """添加知识的可用KDN服务器"""
        unit = self.get_unit(knowledge_id)
        if kdn_addr not in unit.avail_kdn_servers:
            unit.avail_kdn_servers.append(kdn_addr)

    def remove_kdn_for_knowledge(self, knowledge_id: int, kdn_addr: str) -> None:
        """移除知识的可用KDN服务器"""
        unit = self.get_unit(knowledge_id)
        unit.avail_kdn_servers = [x for x in unit.avail_kdn_servers if x != kdn_addr]

    def build_faiss_index(self) -> None:
        """
        根据当前所有 KnowledgeUnit 的 embedding 重建 FAISS 索引。
        """
        if faiss is None:
            print("[KnowledgeTable] faiss not installed; will use python fallback search.")
            self._faiss_index = None
            return

        kids = list(self._units.keys())
        if not kids:
            self._faiss_index = None
            print("[KnowledgeTable] No units to build FAISS index.")
            return

        xb = np.asarray([self._units[k].embedding for k in kids], dtype=np.float32)

        base = faiss.IndexFlatIP(self.dim)  # cosine if embeddings are normalized
        index = faiss.IndexIDMap2(base)

        ids = np.asarray([self._kid_to_i64.get(k) or self._register_kid(k) for k in kids], dtype=np.int64)
        index.add_with_ids(xb, ids)

        self._faiss_index = index
        print(f"[KnowledgeTable] FAISS index built, size={index.ntotal}, dim={self.dim}")

    # --------------------------------------------------
    # ------------------- 向量检索接口 -------------------
    # --------------------------------------------------
    def search_by_embedding(
            self,
            query_embedding: List[float],
            top_k: int,
            min_score: float = 0.25,
            min_ratio: float = 0.75,
    ) -> List[Tuple[str, KnowledgeUnit, float]]:
        """
            用 query_embedding 在整个知识库里做相似度检索。
            返回：
            List[(knowledge_id, unit, score)]，按 score 降序。
            优先使用 FAISS；未安装或索引为空时退回 Python 版本。
        """
        self._check_dim(query_embedding)

        # ---- 分支 1：FAISS 可用 ----
        if not self._units:
            return []

        q = np.asarray([query_embedding], dtype=np.float32)

        def _filter_hits(hits: List[Tuple[str, KnowledgeUnit, float]]) -> List[Tuple[str, KnowledgeUnit, float]]:
            if not hits:
                return []
            best = float(hits[0][2])
            kept: List[Tuple[str, KnowledgeUnit, float]] = []
            for kid, unit, score in hits:
                score = float(score)
                if score < float(min_score):
                    continue
                # best<=0 时 ratio 没意义，仅用 min_score
                if best > 0 and score < best * float(min_ratio):
                    continue
                kept.append((kid, unit, score))
            return kept


        if self._faiss_index is not None:
            D, I = self._faiss_index.search(q, top_k)
            out = []
            for score, i64 in zip(D[0].tolist(), I[0].tolist()):
                if i64 < 0:
                    continue
                kid = self._i64_to_kid.get(int(i64))
                if not kid:
                    continue
                unit = self._units.get(kid)
                if unit is None:
                    continue
                out.append((kid, unit, float(score)))

            return _filter_hits(out)

        # ---- 分支 2：回退到纯 Python 版本 ----
        # fallback: brute force cosine (inner product)
        out = []
        for kid, unit in self._units.items():
            score = float(np.dot(np.asarray(query_embedding, dtype=np.float32), np.asarray(unit.embedding, dtype=np.float32)))
            out.append((kid, unit, score))
        out.sort(key=lambda x: x[2], reverse=True)
        return _filter_hits(out[:top_k])


def init_knowledge_table(
        yaml_path: str,
        embedder: EmbeddingModel,
) -> KnowledgeTable:
    """
       从 YAML 文件中加载知识条目，构建并返回一个 KnowledgeTable 实例。

       参数
       ----
       yaml_path : str YAML 配置文件路径。
       embedder : EmbeddingModel 用于将 text 转为 embedding 的模型实例。
           - 如果 YAML 中某条知识没有直接提供 embedding 字段，则必须依赖 embedder 根据 text 生成。

       知识条目的YAML格式约定（示例）
       ---------------------
       knowledge_dim: 64      # 向量维度，建议显式给出；否则尝试从 embedder.dim 推断

       knowledge_items:
         - id: 1
           length: 512
           text: "该知识块的描述、标题或摘要，用于生成 embedding"
           # embedding: [0.1, 0.2, ...]  # 必须携带
           llm_systems:
             - "10.0.0.11:8000"
             - "10.0.0.12:8000"
           kdn_servers:
             - "10.0.1.21:9000"

         - id: 2                           # 特殊情况下也可以没有id，系统会自动分配
           length: 800
           embedding: [0.01, 0.02, ...]    # 可以没有文本
           llm_systems: []
           kdn_servers: []

       返回
       ----
       KnowledgeTable
           已填充好初始知识单元的知识表对象。
    """

    # 一、读取 YAML 配置文件
    with open(yaml_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # 二、确定向量维度 dim，如果 YAML 没写 knowledge_dim，则尝试从 embedder 上读取，否则报错
    dim = config.get("knowledge_dim", None)
    if dim is None:
        if hasattr(embedder, "dim"):
            dim = int(getattr(embedder, "dim"))
        else:
            raise ValueError(
                "YAML 中未配置 knowledge_dim，且 embedder 无 dim 属性，"
                "无法确定向量维度，请在 YAML 顶层添加 knowledge_dim 字段。"
            )

    # 读取全局默认服务器列表（可选）
    default_servers = config.get("default_servers", []) or []

    # 三、构建 KnowledgeTable
    table = KnowledgeTable(dim=dim, default_servers=default_servers)

    # 四、逐条加载 knowledge_items
    items = config.get("knowledge_items", []) or []
    for item in items:
        # 1) 处理 knowledge_id：有则用之，无则动态分配
        if "id" in item:
            kid = int(item["id"])
        else:
            kid = table.assign_new_id()

        length = int(item.get("length", 0))
        llm_systems: List[str] = list(item.get("llm_systems", []) or [])
        kdn_servers: List[str] = list(item.get("kdn_servers", []) or [])
        text = item.get("text")

        # 2) embedding / text 处理
        embedding = item.get("embedding", None)
        text = item.get("text", None)

        if embedding is None:
            raise ValueError(f"知识条目 id={kid} 缺少 embedding 字段（默认必须有 embedding）")

        # 3) 写入 KnowledgeTable（内部会检查维度）
        table.upsert_knowledge(
            knowledge_id=kid,
            embedding=embedding,
            length=length,
            avail_llm_systems=llm_systems,
            avail_kdn_servers=kdn_servers,
            text_abstract=text,
        )

    # 所有知识条目就绪后，构建 FAISS 索引
    table.build_faiss_index()

    return table



def apply_knowledge_update(
    table: KnowledgeTable,
    embedder: Optional[EmbeddingModel],
    payload: Dict[str, Any],
) -> Optional[int]:
    """
        统一的知识表更新接口。
        - 上层（HTTP / RPC / MQ 消费者）只需要把请求解析成一个 dict，
        - 真正的知识表操作细节都封装在这里。

        payload 约定格式
        ----------------
        1) 新增 / 更新知识单元（upsert）：
           {
             "op": "upsert",
             "knowledge_id": 1,
             "length": 512,                 # 必填
             "embedding": [...],            # 必填
             "text": "该知识块的描述",      # 可选
             "llm_systems": ["10.0.0.11:8000", ...],  # 可选
             "kdn_servers": ["10.0.1.21:9000", ...],  # 可选
           }

        2) 为指定知识增加一个可用 LLM（add_llm）：
           {
             "op": "add_llm",
             "knowledge_id": 1,
             "llm_addr": "10.0.0.13:8000"
           }

        3) 为指定知识移除一个 LLM（remove_llm）：
           {
             "op": "remove_llm",
             "knowledge_id": 1,
             "llm_addr": "10.0.0.13:8000"
           }

        4) 为指定知识增加一个可用 KDN（add_kdn）：
           {
             "op": "add_kdn",
             "knowledge_id": 1,
             "kdn_addr": "10.0.1.23:9000"
           }

        5) 为指定知识移除一个 KDN（remove_kdn）：
           {
             "op": "remove_kdn",
             "knowledge_id": 1,
             "kdn_addr": "10.0.1.23:9000"
           }

        参数
        ----
        table : KnowledgeTable
            要更新的知识表实例。
        embedder : Optional[EmbeddingModel]
            在 op == "upsert" 且未提供 embedding 时，用于根据 text 计算 embedding。
            某些情况下你可能只允许“embedding 必须由外部提供”，那可以传入 None 并在此处抛错。
        payload : Dict[str, Any]
            外部请求解析后的字典。
    """
    op = payload.get("op")
    if not op:
        raise ValueError("知识更新请求缺少 'op' 字段")
    op = str(op).lower() # 规范一下 op 字符串，避免大小写问题

    # ----------- 1) upsert：新增或更新知识单元 -----------
    if op == "upsert":
        # 1) 有 knowledge_id 则更新，无则新建并自动分配
        if "knowledge_id" in payload:
            knowledge_id = int(payload["knowledge_id"])
        else:
            knowledge_id = table.assign_new_id()

        length = int(payload.get("length", 0))
        # 可选字段
        llm_systems = payload.get("llm_systems")
        kdn_servers = payload.get("kdn_servers")
        embedding = payload.get("embedding")
        text = payload.get("text")

        # embedding / text 二选一
        if embedding is None:
            raise ValueError(f"知识条目 id={knowledge_id} 缺少 embedding 字段（默认必须有 embedding）")

        # 真正写入 KnowledgeTable
        table.upsert_knowledge(
            knowledge_id=knowledge_id,
            embedding=embedding,
            length=length,
            avail_llm_systems=llm_systems,
            avail_kdn_servers=kdn_servers,
            text_abstract=text,
        )
        return knowledge_id

    # ----------- 2) add_llm：为某知识增加一个 LLM 地址 -----------
    if op == "add_llm":
        knowledge_id = int(payload["knowledge_id"])
        llm_addr = str(payload["llm_addr"])
        table.add_llm_for_knowledge(knowledge_id, llm_addr)
        return None

    # ----------- 3) remove_llm：为某知识移除一个 LLM 地址 -----------
    if op == "remove_llm":
        knowledge_id = int(payload["knowledge_id"])
        llm_addr = str(payload["llm_addr"])
        table.remove_llm_for_knowledge(knowledge_id, llm_addr)
        return None

    # ----------- 4) add_kdn：为某知识增加一个 KDN 地址 -----------
    if op == "add_kdn":
        knowledge_id = int(payload["knowledge_id"])
        kdn_addr = str(payload["kdn_addr"])
        table.add_kdn_for_knowledge(knowledge_id, kdn_addr)
        return None

    # ----------- 5) remove_kdn：为某知识移除一个 KDN 地址 -----------
    if op == "remove_kdn":
        knowledge_id = int(payload["knowledge_id"])
        kdn_addr = str(payload["kdn_addr"])
        table.remove_kdn_for_knowledge(knowledge_id, kdn_addr)
        return None

    raise ValueError(f"未知的知识更新操作类型 op={op!r},可选类型有upsert, add_llm, remove_llm, add_kdn, remove_kdn")



def print_knowledge_table_state(table: KnowledgeTable) -> None:
    """
        打印当前知识库的状态：
          - knowledge_id
          - text（若有）
          - length
          - llm_systems
          - kdn_servers
          - default_servers
    """
    print("=" * 60)
    print("Current KnowledgeTable State:")
    print("-" * 60)

    # KnowledgeTable 内部是 dict[int, KnowledgeUnit]，只需要遍历即可
    # 具体见Class KnowledgeTable self._units 字段
    units = getattr(table, "_units", {})
    for kid in sorted(units.keys()):
        unit = units[kid]
        print(f"Knowledge ID      : {kid}")
        if unit.text_abstract:
            print(f"  text            : {unit.text_abstract}")
        print(f"  length          : {unit.length}")
        print(f"  llm_systems     : {unit.avail_llm_systems}")
        print(f"  kdn_servers     : {unit.avail_kdn_servers}")
        print(f"  default_servers : {unit.default_servers}")
        print("-" * 60)

    if not units:
        print(" KnowledgeTable is empty")
    print("=" * 60)
    print()
