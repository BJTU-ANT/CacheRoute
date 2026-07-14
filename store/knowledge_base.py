"""
store/knowledge_base.py
=======================
Defines the knowledge-base maintenance unit data structure.
Defines basic knowledge-base unit maintenance methods.

Scheduler knowledge-base startup entry point, responsible for:
  1) reading initial knowledge entries from a YAML file and building a KnowledgeTable;
  2) providing the unified knowledge-table update interface apply_knowledge_update for future integration with:
       - upper-layer protocols such as HTTP API / gRPC / message queues;
       - direct calls from scheduler internal modules.

Dependencies:
  - base.py defines:
      * KnowledgeTable
  - embedding_index_method.py defines:
      * EmbeddingModel(specific implementations, for example DummyEmbeddingModel)
  - pyyaml is used to parse YAML configuration files
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
    raise ImportError("pyyaml must be installed to support YAML config parsing: pip install pyyaml") from e

try:
    import faiss
except ImportError:
    faiss = None


@dataclass
class KnowledgeUnit:
    """
        Represents information for one knowledge chunk, excluding the full knowledge text:
        Class KnowledgeUnit
          |- embedding: high-dimensional vector List[float] representing the knowledge chunk, used for vector retrieval to locate chunk information
          |- length: knowledge chunk length, such as token count or character count; user-defined
          |- avail_llm_systems: list of available LLM system addresses,e.g., ["llm://10.0.0.3:8000"]
          |- avail_kdn_servers: list of available KDN server addresses,e.g., ["kdn://10.0.1.5:9000"]
          |- text_abstract: optional text description/summary of the knowledge, used only for display and debugging
          |- default_servers: default servers available to all knowledge units, injected by KnowledgeTable
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
    # Optional: keep the full text so the scheduler can estimate token length with the target model tokenizer.
    full_content: Optional[str] = None


class KnowledgeTable:
    """
    Knowledge-base table:
      - maintains the knowledge_id -> KnowledgeUnit mapping
      - provides update APIs to add/remove LLMs or KDNs for a knowledge item
      - dynamically updates knowledge_id
      - provides an embedding-based similarity search interface

    Note:
      This uses the simplest in-memory + cosine-similarity implementation.
      You can later replace it with FAISS as long as the search_by_embedding interface remains unchanged.
    """

    def __init__(self, dim: int, default_servers: List[str] = None):
        self.dim = dim
        # External mapping: kid(str) -> KnowledgeUnit
        self._units: Dict[str, KnowledgeUnit] = {}
        self._next_id = 0

        # Global default text-injection servers available to all knowledge units
        self._default_servers : List[str] =list(default_servers or [])

        # Internal mapping: FAISS needs int64 IDs, so map kid <-> int64 transparently to callers
        self._kid_to_i64: Dict[str, int] = {}
        self._i64_to_kid: Dict[int, str] = {}

        # ---- New: FAISS-index related state ----
        self._faiss_index = None
        self._faiss_ids: List[int] = []  # row_idx -> knowledge_id mapping


    # ----------------------------------------------
    # ------------------- Basic utilities ------------------
    # ----------------------------------------------
    def _check_dim(self, vec: Sequence[float]):
        """Check whether the embedding dimension is correct."""
        if len(vec) != self.dim:
            raise ValueError(f"Embedding dim mismatch: expect {self.dim}, got {len(vec)}")

    @staticmethod
    def _cosine_similarity(vec1: Sequence[float], vec2: Sequence[float]) -> float:
        """Compute cosine similarity between two vectors as the retrieval score."""
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
        """Allocate an increasing ID for new knowledge that has no explicit ID."""
        kid = self._next_id
        self._next_id += 1
        return kid

    def _update_next_id_with_existing(self, knowledge_id: int) -> None:
        """
            When an external caller explicitly specifies knowledge_id, for example id=5 read from YAML,
            ensure _next_id is always greater than the current maximum ID.
        """
        if knowledge_id >= self._next_id:
            self._next_id = knowledge_id + 1

    def get_llm_parsed(self, knowledge_id: int, default_port: int = 7000):
        """Parse address strings for the specified knowledge ID and return [(ip, port), (ip, port), ...]."""
        unit = self._units.get(knowledge_id)
        return [parse_host_port(addr, default_port) for addr in unit.avail_llm_systems]

    def get_kdn_parsed(self, knowledge_id: int, default_port: int = 8000):
        """Parse address strings for the specified knowledge ID and return [(ip, port), (ip, port), ...]."""
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
        Collision fallback logic to avoid duplicate int64 collisions.
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
    # ------------------- Knowledge management interface -------------------
    # --------------------------------------------------
    def upsert_kid(self, kid: str, unit: KnowledgeUnit) -> None:
        """
        upsert_knowledge is the early local-YAML knowledge-base maintenance method; in the new version, only this method should be used.
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
            Add or update one knowledge chunk.
            If knowledge_id already exists, overwrite the original record;
            if it is a new ID, insert it and update _next_id.
            The scheduler or backend management program can call this interface to maintain the knowledge base dynamically.
        """
        self._check_dim(embedding)

        unit = KnowledgeUnit(
            embedding=list(embedding),
            length=int(length),
            avail_llm_systems=list(avail_llm_systems or []),
            avail_kdn_servers=list(avail_kdn_servers or []),
            text_abstract=text_abstract,
            # Each knowledge unit automatically inherits the current global default server list
            default_servers=list(self._default_servers),
        )
        self._units[knowledge_id] = unit
        # Ensure next_id is always greater than the maximum used ID
        self._update_next_id_with_existing(knowledge_id)

    def get_unit(self, knowledge_id: int) -> KnowledgeUnit:
        """Get unit information for a knowledge ID."""
        return self._units[knowledge_id]

    def add_llm_for_knowledge(self, knowledge_id: int, llm_addr: str) -> None:
        """Update available LLM systems for the knowledge item."""
        unit = self.get_unit(knowledge_id)
        if llm_addr not in unit.avail_llm_systems:
            unit.avail_llm_systems.append(llm_addr)

    def remove_llm_for_knowledge(self, knowledge_id: int, llm_addr: str) -> None:
        """Remove available LLM systems from the knowledge item."""
        unit = self.get_unit(knowledge_id)
        unit.avail_llm_systems = [x for x in unit.avail_llm_systems if x != llm_addr]

    def add_kdn_for_knowledge(self, knowledge_id: int, kdn_addr: str) -> None:
        """Add available KDN servers for the knowledge item."""
        unit = self.get_unit(knowledge_id)
        if kdn_addr not in unit.avail_kdn_servers:
            unit.avail_kdn_servers.append(kdn_addr)

    def remove_kdn_for_knowledge(self, knowledge_id: int, kdn_addr: str) -> None:
        """Remove available KDN servers from the knowledge item."""
        unit = self.get_unit(knowledge_id)
        unit.avail_kdn_servers = [x for x in unit.avail_kdn_servers if x != kdn_addr]

    def build_faiss_index(self) -> None:
        """
        Rebuild the FAISS index from embeddings of all current KnowledgeUnits.
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
    # ------------------- Vector retrieval interface -------------------
    # --------------------------------------------------
    def search_by_embedding(
            self,
            query_embedding: List[float],
            top_k: int,
            min_score: float = 0.25,
            min_ratio: float = 0.75,
    ) -> List[Tuple[str, KnowledgeUnit, float]]:
        """
            Run similarity search over the full knowledge base with query_embedding.
            Returns:
            List[(knowledge_id, unit, score)], sorted by descending score.
            Prefer FAISS; fall back to the Python version when FAISS is not installed or the index is empty.
        """
        self._check_dim(query_embedding)

        # ---- Branch 1: FAISS is available ----
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
                # When best <= 0, ratio is meaningless; use only min_score
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

        # ---- Branch 2: fall back to the pure Python version ----
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
       Load knowledge entries from a YAML file, build a KnowledgeTable instance, and return it.

       Parameters
       ----
       yaml_path : str Path to the YAML configuration file.
       embedder : EmbeddingModel Model instance used to convert text to embeddings.
           - If a YAML knowledge item does not directly provide an embedding field, embedder must generate one from text.

       Knowledge item YAML format convention (example)
       ---------------------
       knowledge_dim: 64      # vector dimension; explicitly setting it is recommended, otherwise infer from embedder.dim

       knowledge_items:
         - id: 1
           length: 512
           text: "description, title, or summary of this knowledge chunk, used to generate embeddings"
           # embedding: [0.1, 0.2, ...]  # must be provided
           llm_systems:
             - "10.0.0.11:8000"
             - "10.0.0.12:8000"
           kdn_servers:
             - "10.0.1.21:9000"

         - id: 2                           # id may be omitted in special cases; the system will allocate one automatically
           length: 800
           embedding: [0.01, 0.02, ...]    # text may be omitted
           llm_systems: []
           kdn_servers: []

       Returns
       ----
       KnowledgeTable
           KnowledgeTable object filled with initial knowledge units.
    """

    # 1. Read the YAML configuration file
    with open(yaml_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # 2. Determine vector dimension dim; if YAML lacks knowledge_dim, try to read it from embedder, otherwise raise an error
    dim = config.get("knowledge_dim", None)
    if dim is None:
        if hasattr(embedder, "dim"):
            dim = int(getattr(embedder, "dim"))
        else:
            raise ValueError(
                "YAML does not configure knowledge_dim, and embedder has no dim attribute; "
                "cannot determine vector dimension. Please add a top-level knowledge_dim field to YAML."
            )

    # Read the global default server list (optional)
    default_servers = config.get("default_servers", []) or []

    # 3. Build KnowledgeTable
    table = KnowledgeTable(dim=dim, default_servers=default_servers)

    # 4. Load knowledge_items one by one
    items = config.get("knowledge_items", []) or []
    for item in items:
        # 1) Handle knowledge_id: use it if present; otherwise allocate dynamically
        if "id" in item:
            kid = int(item["id"])
        else:
            kid = table.assign_new_id()

        length = int(item.get("length", 0))
        llm_systems: List[str] = list(item.get("llm_systems", []) or [])
        kdn_servers: List[str] = list(item.get("kdn_servers", []) or [])
        text = item.get("text")

        # 2) embedding / text processing
        embedding = item.get("embedding", None)
        text = item.get("text", None)

        if embedding is None:
            raise ValueError(f"knowledge item id={kid} missing embedding field (embedding is required by default)")

        # 3) Write into KnowledgeTable; dimensions are checked internally
        table.upsert_knowledge(
            knowledge_id=kid,
            embedding=embedding,
            length=length,
            avail_llm_systems=llm_systems,
            avail_kdn_servers=kdn_servers,
            text_abstract=text,
        )

    # After all knowledge items are ready, build the FAISS index
    table.build_faiss_index()

    return table



def apply_knowledge_update(
    table: KnowledgeTable,
    embedder: Optional[EmbeddingModel],
    payload: Dict[str, Any],
) -> Optional[int]:
    """
        Unified knowledge-table update interface.
        - Upper layers (HTTP / RPC / MQ consumers) only need to parse the request into a dict,
        - and the actual knowledge-table operation details are encapsulated here.

        Payload format convention
        ----------------
        1) Add/update knowledge unit (upsert):
           {
             "op": "upsert",
             "knowledge_id": 1,
             "length": 512,                 # required
             "embedding": [...],            # required
             "text": "description of this knowledge chunk",      # optional
             "llm_systems": ["10.0.0.11:8000", ...],  # optional
             "kdn_servers": ["10.0.1.21:9000", ...],  # optional
           }

        2) Add an available LLM for the specified knowledge (add_llm):
           {
             "op": "add_llm",
             "knowledge_id": 1,
             "llm_addr": "10.0.0.13:8000"
           }

        3) Remove an LLM from the specified knowledge (remove_llm):
           {
             "op": "remove_llm",
             "knowledge_id": 1,
             "llm_addr": "10.0.0.13:8000"
           }

        4) Add an available KDN for the specified knowledge (add_kdn):
           {
             "op": "add_kdn",
             "knowledge_id": 1,
             "kdn_addr": "10.0.1.23:9000"
           }

        5) Remove a KDN from the specified knowledge (remove_kdn):
           {
             "op": "remove_kdn",
             "knowledge_id": 1,
             "kdn_addr": "10.0.1.23:9000"
           }

        Parameters
        ----
        table : KnowledgeTable
            KnowledgeTable instance to update.
        embedder : Optional[EmbeddingModel]
            When op == "upsert" and no embedding is provided, this is used to compute embedding from text.
            In some cases, you may require embeddings to be externally provided; pass None and raise an error here.
        payload : Dict[str, Any]
            Dict parsed from the external request.
    """
    op = payload.get("op")
    if not op:
        raise ValueError("knowledge update request is missing the 'op' field")
    op = str(op).lower() # Normalize the op string to avoid case-sensitivity issues

    # ----------- 1) upsert:add or update a knowledge unit -----------
    if op == "upsert":
        # 1) update when knowledge_id exists; otherwise create and allocate automatically
        if "knowledge_id" in payload:
            knowledge_id = int(payload["knowledge_id"])
        else:
            knowledge_id = table.assign_new_id()

        length = int(payload.get("length", 0))
        # Optional fields
        llm_systems = payload.get("llm_systems")
        kdn_servers = payload.get("kdn_servers")
        embedding = payload.get("embedding")
        text = payload.get("text")

        # embedding / text choose one of the two
        if embedding is None:
            raise ValueError(f"knowledge item id={knowledge_id} missing embedding field (embedding is required by default)")

        # Actually write into KnowledgeTable
        table.upsert_knowledge(
            knowledge_id=knowledge_id,
            embedding=embedding,
            length=length,
            avail_llm_systems=llm_systems,
            avail_kdn_servers=kdn_servers,
            text_abstract=text,
        )
        return knowledge_id

    # ----------- 2) add_llm: add one LLM address for a knowledge item -----------
    if op == "add_llm":
        knowledge_id = int(payload["knowledge_id"])
        llm_addr = str(payload["llm_addr"])
        table.add_llm_for_knowledge(knowledge_id, llm_addr)
        return None

    # ----------- 3) remove_llm: remove one LLM address from a knowledge item -----------
    if op == "remove_llm":
        knowledge_id = int(payload["knowledge_id"])
        llm_addr = str(payload["llm_addr"])
        table.remove_llm_for_knowledge(knowledge_id, llm_addr)
        return None

    # ----------- 4) add_kdn: add one KDN address for a knowledge item -----------
    if op == "add_kdn":
        knowledge_id = int(payload["knowledge_id"])
        kdn_addr = str(payload["kdn_addr"])
        table.add_kdn_for_knowledge(knowledge_id, kdn_addr)
        return None

    # ----------- 5) remove_kdn: remove one KDN address from a knowledge item -----------
    if op == "remove_kdn":
        knowledge_id = int(payload["knowledge_id"])
        kdn_addr = str(payload["kdn_addr"])
        table.remove_kdn_for_knowledge(knowledge_id, kdn_addr)
        return None

    raise ValueError(f"unknown knowledge update operation type op={op!r},valid operations are upsert, add_llm, remove_llm, add_kdn, remove_kdn")



def print_knowledge_table_state(table: KnowledgeTable) -> None:
    """
        Print the current knowledge-base status:
          - knowledge_id
          - text(if present)
          - length
          - llm_systems
          - kdn_servers
          - default_servers
    """
    print("=" * 60)
    print("Current KnowledgeTable State:")
    print("-" * 60)

    # KnowledgeTable internally uses dict[int, KnowledgeUnit], so iterating it is enough
    # See the KnowledgeTable class self._units field for details
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
