# Build task and task-batch information structures.
import time
from dataclasses import dataclass, asdict
from core.tokenizer_registry import estimate_tokens
from core.config import (
    DEFAULT_EMBED_MODEL,
    SCHEDULER_RETRIEVAL_TOP_K,
    SCHEDULER_RETRIEVAL_MIN_SCORE,
    SCHEDULER_RETRIEVAL_MIN_RATIO,
)
# from CacheRoute.util import parse_host_port
from typing import Dict, Any, List, Tuple, Optional
from store import EmbeddingModel, KnowledgeTable
from util import parse_stream_flag

"""
Defines the scheduler-to-local-Proxy interaction data structure -> Class Request.
The scheduler builds a request through scheduling strategies and sends it to the
local Proxy; the Proxy then performs the required actions to complete pool
selection and inference.

    Class Request:
      |  Request_ID: unique identifier for the user task
      |__Request_type: request type, such as request, control, update, etc.
      |
      |__Class Prompt: records basic user-question information, such as the
      |   specific question, model type, and question length.
      |   |  model                      model type
      |   |  user_prompt                user question
      |   |  token_length               token length of the question
      |   |  max_token                  maximum number of generated tokens
      |   |  stream                     whether streaming is enabled
      |   |  temperature                sampling temperature, default 1.0
      |   |__top_p                      nucleus sampling truncation threshold, default 1.0
      |
      |__Class Service: maps user-question service requirements, such as whether
      |   PD disaggregation and knowledge injection are supported, plus TTFT,
      |   E2E, and TPOT SLO requirements.
      |   |  Enable_PD_Disaggregation   whether PD disaggregation is enabled
      |   |  Enable_know_injection      whether knowledge injection is enabled
      |   |  Injection_type             knowledge injection type (text or KV)
      |   |  Enable_compress            whether KVCache compression is allowed
      |   |  Compress_factor            compression ratio
      |   |  Enable_security            whether security mode is enabled (reserved)
      |   |  Knowledge_block_num        number of knowledge blocks
      |   |  Knowledge_List             knowledge list required by the question
      |   |  Knowledge_length           knowledge injection length
      |   |  SLO_TTFT                   time to first token
      |   |  SLO_E2E                    end-to-end latency
      |   |  SLO_TPOT                   token-per-output-token latency target
      |   |__Endpoint_type              request type forwarded to vLLM, including
      |                              chat/completions or completions
      |
      |__Class Task: records scheduling-related task information, such as KDN
      |   knowledge-server IP address, PD-pool Proxy IP address, and ports.
          |  User_addr                  user IP address
          |  KDN_server_addr            KDN server address
          |  default_know_addr          default text-injection server address
          |  P_proxy_id                 selected Proxy id
          |  P_proxy_addr               P-pool Proxy IP address
          |  D_proxy_addr               D-pool Proxy IP address
          |  P_proxy_port               P-pool Proxy port
          |  D_proxy_port               D-pool Proxy port
          |  prefill_instance           Prefill instance IP address and port assigned to this request
          |  decode_instance            Decode instance IP address and port assigned to this request
          |__batch_order                batch order of this request on its assigned instance
"""

# ========================================================================================================================
# ------------------------------------------------------Prompt------------------------------------------------------------
# ========================================================================================================================
@dataclass
class Prompt:
    """
        Defines basic information for the user question.
            model<task model, str>
            user_prompt<user question, str>
            token_length<token length of the question>
            bs<batch_size of the task group, usually defaults to 1>
            max_token<maximum number of generated tokens supported by the task>
            stream<whether streaming is enabled>
            temperature<sampling temperature, default 1.0>
            top_p<nucleus sampling truncation threshold, default 1.0>
    """
    model: str
    user_prompt: str
    token_length: int
    bs: int

    max_tokens: int
    stream: bool
    temperature: float
    top_p: float


    @classmethod
    def extract_prompt_info(cls, model: str, user_prompt: str) -> int:
        """
            Automatically calculates token_length from user_prompt and returns it.
            Input: model, user_prompt
            Output: token_length
        """
        seq_length = estimate_tokens(user_prompt, model)
        print(f"[Prompt-tokenizer]: get task prompt_length complete, prompt_length={seq_length}")

        return seq_length


# ========================================================================================================================
# ------------------------------------------------------Service-----------------------------------------------------------
# ========================================================================================================================
@dataclass
class Service:
    """
        Defines basic SLO information for question service requirements. The user
        IP address maps to a concrete service level, and the mapping module can
        later be extended for security, personalization, and related features.
            Enable_PD_Disaggregation<whether PD disaggregation is allowed, default True>
            Enable_know_injection<whether remote knowledge is allowed, default True>
            Injection_type<knowledge injection type, default text; Proxy may adjust it later by policy>
            Enable_compress<whether KVCache compression is allowed, default True>
            Compress_factor<KVCache compression ratio, default 0.3; options include 0.3, 0.5, and 0.7>
            Enable_security<whether security mode is enabled, default false; reserved for later security features>
            Knowledge_block_num<top number of knowledge blocks injected for the task, default 3>
            Knowledge_List<knowledge block list whose elements are Knowledge_ID values for concrete blocks>
            Knowledge_length<token length of the task-injected knowledge, default 0>
            SLO_TTFT<TTFT SLO requirement for the task group, from inference start to first token, default 2000ms>
            SLO_E2E<full time from Prefill start to Decode end, in ms>
            SLO_TPOT<TPOT SLO requirement for the task group, average autoregressive decode latency, default 20ms>
            Endpoint_type<request type forwarded to vLLM, including chat/completions or completions>
    """
    Enable_PD_Disaggregation: bool
    Enable_know_injection: bool
    Injection_type: str
    Enable_compress: bool
    Compress_factor: float
    Enable_security: bool
    Knowledge_block_num: int
    Knowledge_List: List[str]
    Knowledge_length: int
    SLO_TTFT: int
    SLO_E2E: int
    SLO_TPOT: int
    Endpoint_type: str


    @classmethod
    def mapping_slo_info(cls, user_addr: str) -> Dict[str, Any]:
        """
            Maps the user IP address to concrete service SLOs and enabled features.
            Input: user_addr
            Output: service SLOs such as TTFT, TPOT, and E2E, plus feature flags
                    such as whether PD disaggregation, compression, and security
                    are enabled.
        """
        if user_addr.startswith("10.0."):
            return {
                "Enable_PD_Disaggregation": False,
                "SLO_TTFT": 5000,
                "SLO_TPOT": 50,
                "SLO_E2E": 55000,
            }
        elif user_addr.startswith("192.168."):
            return {
                "Enable_PD_Disaggregation": False,
                "SLO_TTFT": 10000,
                "SLO_TPOT": 150,
                "SLO_E2E": 160000,
            }
        else:
            return {
                "Enable_PD_Disaggregation": True,
                "SLO_TTFT": 15000,
                "SLO_TPOT": 200,
                "SLO_E2E": 215000,
            }


    @classmethod
    def knowledge_retriever(
            cls,
            user_prompt: str,
            knowledge_block_num: int,
            embedder: EmbeddingModel,
            knowledge_table: KnowledgeTable,
            model_name: str,
    ) -> Tuple[List[str], int]:
        """
            Retrieves the most relevant knowledge list from the knowledge base
            according to the user question.

            Args:
                user_prompt: str -> user question used to generate the query embedding
                knowledge_block_num: int -> desired number of returned knowledge blocks (top-k)
                embedder: EmbeddingModel -> externally injected embedding model
                knowledge_table: KnowledgeTable -> instance whose FAISS index has already been built

            Returns:
                knowledge_ids: list of retrieved knowledge block IDs (length <= knowledge_block_num)
                total_knowledge_length: total token length of these knowledge blocks
        """
        user_prompt = (user_prompt or "").strip()
        if not user_prompt:
            # Empty prompts return an empty result immediately.
            return [], 0

        if knowledge_block_num <= 0:
            return [], 0

        print(f"[Knowledge retriever]: use embedder={DEFAULT_EMBED_MODEL}")

        # 1) Encode user_prompt into an embedding.
        query_embedding = embedder.encode_vector([user_prompt])[0]

        # 2) Retrieve top-k knowledge blocks; FAISS is preferred with Python scan fallback.
        results = knowledge_table.search_by_embedding(
            query_embedding=query_embedding,
            top_k=knowledge_block_num,
            min_score=float(SCHEDULER_RETRIEVAL_MIN_SCORE),
            min_ratio=float(SCHEDULER_RETRIEVAL_MIN_RATIO),
        )

        # 3) Extract the id list and total length.
        knowledge_ids: List[str] = []
        total_len = 0
        for kid, unit, score in results:
            knowledge_ids.append(kid)
            stored_len = int(getattr(unit, "length", 0) or 0)
            full_content = str(getattr(unit, "full_content", "") or "")
            raw_text = full_content if full_content else str(getattr(unit, "text_abstract", "") or "")

            # Support two source types:
            # 1) KDN sync path: full_content contains complete content, and length is usually character length.
            # 2) Local YAML path: text_abstract may be only a summary and cannot always be tokenized directly.
            #
            # If full_content exists, tokenize it directly; otherwise keep compatibility checks.
            if full_content:
                looks_like_full_content = True
            elif raw_text and stored_len > 0:
                length_gap = abs(len(raw_text) - stored_len)
                looks_like_full_content = length_gap <= max(32, int(stored_len * 0.1))
            else:
                looks_like_full_content = False

            if looks_like_full_content:
                token_len = Prompt.extract_prompt_info(model=model_name, user_prompt=raw_text)
                total_len += int(token_len)
            else:
                total_len += stored_len

        # Lightweight debug output.
        print(
            "[Service.knowledge_retriever] user_prompt_len={}, "
            "top_k={}, hit={}, knowledge_list_ids={}, total_len={}".format(
                len(user_prompt),
                knowledge_block_num,
                len(knowledge_ids),
                knowledge_ids,
                total_len,
            )
        )

        return knowledge_ids, total_len


def normalize_injection_type(value: Any) -> str:
    """
    Normalize knowledge injection type.

    - Missing or empty values default to kvcache.
    - Common aliases kvcache, kv, kv_cache, and kv-cache map to kvcache.
    - text and prompt map to text.
    - Invalid values fall back to kvcache.
    """
    if value is None:
        return "kvcache"

    s = str(value).strip().lower()
    if not s:
        return "kvcache"

    if s in ("kvcache", "kv", "kv_cache", "kv-cache"):
        return "kvcache"

    if s in ("text", "prompt"):
        return "text"

    print(f"[Request] Unknown Injection_type={value!r}, fallback to 'kvcache'")
    return "kvcache"
# ========================================================================================================================
# ------------------------------------------------------Task--------------------------------------------------------------
# ========================================================================================================================
@dataclass
class Task:
    """
        Defines basic task-scheduling information.
            User_addr<user IP address, representing user identity>
            KDN_server_addr<best KDN server address selected for the task according to knowledge needs>
            default_know_addr<default knowledge-injection server, using text injection>
            P_proxy_id<Proxy ID that handles the task, used for flow tracing>
            P_proxy_addr<P-pool Proxy IP address that handles the task, defaulting to local loopback>
            P_proxy_port<P-pool Proxy port that handles the task, defaulting to 8001>
            D_proxy_addr<D-pool Proxy IP address that handles the task, defaulting to local loopback>
            D_proxy_port<D-pool Proxy port that handles the task, defaulting to 8001>
            prefill_instance<Prefill instance IP address and port assigned to this request>
            decode_instance<Decode instance IP address and port assigned to this request>
            batch_order<batch order of this request on its assigned instance>
    """
    User_addr: str
    KDN_server_addr: str
    default_know_addr: str
    P_proxy_id: str
    P_proxy_addr: str
    P_proxy_port: int
    D_proxy_addr: str
    D_proxy_port: int
    prefill_instance: str
    decode_instance: str
    batch_order: int
    User_url_path: str


# ========================================================================================================================
# ------------------------------------------------------Request-----------------------------------------------------------
# ========================================================================================================================
@dataclass
class Request:
    """
        Main structure that describes all task requirements.
            Request_ID<unique identifier for the user task>
            Request_type<request type, such as request, control, update, etc.>
            Prompt<information used to process user content>
            Service<information used to process user service requirements>
            Task<information used when scheduling the task>
    """
    Request_ID: int
    Request_type: str
    Prompt: Prompt
    Service: Service
    Task: Task


    @classmethod
    def build_request(
            cls,
            url_path: str,
            payload: Dict,
            user_addr: str,
            request_id: int,
            embedder: Optional[EmbeddingModel] = None,
            knowledge_table: Optional[KnowledgeTable] = None,
            proxies: Optional[List[Dict[str, Any]]] = None,
            kdns: Optional[List[Dict[str, Any]]] = None,
            strategy: Optional[Any] = None,
            kdn_knowledge_index: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    ) -> "Request":
        """
            Converts the raw user request payload into a complete Request object.
            url_path: URL address sent by the user, for example http://127.0.0.1:7001/v1/chat/completions
            Compatible payload formats:
                1) Legacy TCP protocol plain payload: {"model": "...", "user_prompt": "..."}
                2) /v1/chat/completions style: {
                    "model": "...",
                    "messages": [
                        {"role": "system", "content": "..."},
                        {"role": "user",   "content": "..."},
                        ...],
                    "max_tokens": 1024,
                    "temperature": 0.7,
                    "top_p": 0.95,
                    "stream": true,
                    ...}
                3) /v1/completions style: {
                    "model": "...",
                    "prompt": "...." or ["...","..."],
                    "max_tokens": 1024,
                    "temperature": 0.7,
                    "top_p": 0.95,
                    "stream": false,
                    ...}
            user_addr: client IP obtained by the scheduler from the TCP / HTTP layer
            request_id: request ID assigned by the scheduler (can remain default 0,
                        then be overwritten by the scheduler later)
        """

        # print("[BuildRequest] Receive user request payload:", payload)
        model = payload.get("model")
        if not model:
            raise ValueError("Request payload is missing required field 'model'")

        # Extract user_prompt while preserving compatibility with multiple request formats.
        user_prompt: Optional[str] = None
        t0 = time.perf_counter()
        # Type A: legacy protocol provides user_prompt directly over TCP.
        if "user_prompt" in payload:
            user_prompt = str(payload["user_prompt"])

        # Type B: chat/completions style uses messages.
        elif "messages" in payload:
            msgs = payload.get("messages") or []
            if not isinstance(msgs, list):
                raise ValueError("'messages' field must be a list")

            last_user: Optional[str] = None
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                role = m.get("role", "")
                content = m.get("content", "")
                if role == "user":
                    # Keep overwriting so the last user message wins.
                    last_user = str(content)

            if last_user is not None:
                user_prompt = last_user
            else:
                # If there is no user role, fall back to joining all messages.
                pieces: List[str] = []
                for m in msgs:
                    if not isinstance(m, dict):
                        continue
                    r = m.get("role", "")
                    c = m.get("content", "")
                    pieces.append(f"{r}: {c}")
                if pieces:
                    user_prompt = "\n".join(pieces)

        # Type C: completions style uses prompt.
        elif "prompt" in payload:
            prompt = payload.get("prompt")
            if isinstance(prompt, str):
                user_prompt = prompt
            elif isinstance(prompt, list) and prompt:
                # Use the first element for now; callers can change this to concatenation later if needed.
                user_prompt = str(prompt[0])

        if not user_prompt:
            raise ValueError("Unable to parse user_prompt from request; missing user_prompt/messages/prompt")

        # ---- Common request parameters: max_tokens / stream / temperature / top_p ----
        # Use defaults when parameters are absent.
        max_tokens = int(payload.get("max_tokens", 1000) or 1000)
        stream = parse_stream_flag(payload.get("stream"))
        rag = parse_stream_flag(payload.get("RAG"))
        injection_type = normalize_injection_type(payload.get("Injection_type"))

        try:
            temperature = float(payload.get("temperature", 1.0))
        except (TypeError, ValueError):
            temperature = 1.0
        try:
            top_p = float(payload.get("top_p", 1.0))
        except (TypeError, ValueError):
            top_p = 1.0
        # Clamp simple numeric settings to avoid invalid values.
        if temperature <= 0:
            temperature = 1.0
        if not (0 < top_p <= 1.0):
            top_p = 1.0

        # ---- Request_type defaults to "request" and can later extend to control/update. ----
        request_type = payload.get("Request_type", "request")

        print("=============================\n"
              "   Start to build request.\n"
              "=============================")
        # print("[Request] get request ID:", request_id)
        # print("[Request] model:", model)
        # print("[Request] user_prompt:", user_prompt)

        # =========================
        # ---- Build Prompt object ----
        # =========================
        # Prompt construction starts with token estimation.
        tokens = Prompt.extract_prompt_info(model, user_prompt)
        prompt_obj = Prompt(
            model=model,
            user_prompt=user_prompt,
            bs=1,
            token_length=tokens,
            max_tokens=max_tokens,
            stream=stream,
            temperature=temperature,
            top_p=top_p,
        )

        # ===========================
        # ---- Build Service object ----
        # ===========================
        slo_mapping = Service.mapping_slo_info(user_addr)
        service_obj = Service(
            Enable_PD_Disaggregation = slo_mapping["Enable_PD_Disaggregation"],
            Enable_know_injection=rag,
            SLO_TTFT = slo_mapping["SLO_TTFT"],
            SLO_TPOT = slo_mapping["SLO_TPOT"],
            SLO_E2E = slo_mapping["SLO_E2E"],
            Knowledge_block_num = int(SCHEDULER_RETRIEVAL_TOP_K),
            Knowledge_List= [],
            Knowledge_length= 0,
            Enable_security = False,
            Compress_factor = 0.3,
            Enable_compress = True,
            Injection_type=injection_type,
            Endpoint_type="",
        )

        # Determine endpoint type.
        if "messages" in payload:
            service_obj.Endpoint_type = "chat/completions"
        else:
            service_obj.Endpoint_type = "completions"

        # Knowledge retrieval fills Knowledge_List and Knowledge_length.
        if (
                service_obj.Enable_know_injection
                and embedder is not None
                and knowledge_table is not None
        ):
            try:
                knowledge_ids, total_len = Service.knowledge_retriever(
                    user_prompt=user_prompt,
                    knowledge_block_num=service_obj.Knowledge_block_num,
                    embedder=embedder,
                    knowledge_table=knowledge_table,
                    model_name=model,
                )

                service_obj.Knowledge_List = knowledge_ids
                service_obj.Knowledge_length = total_len
            except Exception as e:
                print(f"[Service] knowledge_retriever failed: {e}")
                # Keep defaults on retrieval errors.
                service_obj.Knowledge_List = []
                service_obj.Knowledge_length = 0
        else:
            # Keep defaults when knowledge injection is disabled or the knowledge table is uninitialized.
            if not service_obj.Enable_know_injection:
                print("[Service] Ban knowledge task, skip retriever!")
            elif knowledge_table is None:
                print("[Service] Not found available knowledge table, skip retriever, please check whether KDN servers is up!")
            service_obj.Knowledge_List = []
            service_obj.Knowledge_length = 0


        # ==========================
        # ---- Build Task object ----
        # ==========================
        task_obj = Task(
            User_addr=user_addr,
            KDN_server_addr = "",
            default_know_addr = "",  # Reserved for a default knowledge server; adjust when needed.
            P_proxy_id= "",
            P_proxy_addr = "",
            P_proxy_port = 0,
            D_proxy_addr = "",
            D_proxy_port = 0,
            prefill_instance = "0",
            decode_instance = "",
            batch_order = 0,
            User_url_path=url_path
        )

        # If scheduler provides a strategy and proxy list, select targets inside build_request.
        try:
            if strategy and hasattr(strategy, "select"):
                # Unified strategy API: strategy should provide select(proxies, request_obj_like) or select(proxies, payload).
                # Pass minimal context to avoid making build_request depend on scheduler Request types.
                request_ctx = {
                    "request_id": request_id,
                    "knowledge_list": list(service_obj.Knowledge_List or []),
                    "knowledge_length": int(service_obj.Knowledge_length or 0),
                    "endpoint_type": service_obj.Endpoint_type,
                    # Injection_type is mainly for debugging; production strategies should not hard-branch on it.
                    "injection_type": service_obj.Injection_type,
                    "rag_enabled": bool(service_obj.Enable_know_injection),
                    "prompt_token_length": int(prompt_obj.token_length or 0),
                    "user_prompt": user_prompt,
                    # These contexts let strategies reuse scheduler-maintained data and avoid repeated embedding or online state assembly.
                    "knowledge_table": knowledge_table,
                    "kdn_knowledge_index": kdn_knowledge_index or {},
                }
                chosen_kdn, chosen_proxy = strategy.select(
                    kdns=kdns or [],
                    proxies=proxies or [],
                    payload=payload,
                    url_path=url_path,
                    user_addr=user_addr,
                    request_ctx=request_ctx,
                )

                if chosen_kdn:
                    # Prefer a normalized HTTP base URL for direct KDN client use later.
                    task_obj.KDN_server_addr = f"{chosen_kdn['host']}:{int(chosen_kdn['port'])}"

                if chosen_proxy:
                    task_obj.P_proxy_id = chosen_proxy["proxy_id"]
                    task_obj.P_proxy_addr = chosen_proxy["host"]
                    task_obj.P_proxy_port = int(chosen_proxy["port"])

        except Exception as e:
            print(f"[Scheduler-Strategy]: select failed, fallback to default. err={e}")


        # =========================
        # ---- Assemble final Request ----
        # =========================
        request_obj = Request(
            Request_type=request_type,
            Request_ID=request_id,
            Prompt=prompt_obj,
            Service=service_obj,
            Task=task_obj
        )

        print("=============================\n"
              "   Finish building.\n"
              "=============================")

        return request_obj


    def to_payload(self) -> Dict[str, Any]:
        """
            Serializes the Request object into a payload that can be sent over HTTP.
                Uses dataclasses by default to preserve the full structure:
                  {
                    "Request_ID": ...,
                    "Request_type": ...,
                    "Prompt": { ... },
                    "Service": { ... },
                    "Task": { ... }
                  }
        """
        return asdict(self)


    def __repr__(self):
        return (
            f"Request(\n"
            f"  Request_ID={self.Request_ID},\n"
            f"  Request_type={self.Request_type},\n"
            f"--------Prompt--------\n"
            f"  model={self.Prompt.model},\n"
            f"  user_prompt={self.Prompt.user_prompt},\n"
            f"  token_length={self.Prompt.token_length},\n"
            f"  stream={self.Prompt.stream},\n"
            f"  temperature={self.Prompt.temperature},\n"
            f"  top_p={self.Prompt.top_p},\n"
            f"  max_tokens={self.Prompt.max_tokens},\n"
            f"--------Service--------\n"
            f"  Enable_PD={self.Service.Enable_PD_Disaggregation},\n"
            f"  Enable_know_injection={self.Service.Enable_know_injection},\n"
            f"  Injection_type={self.Service.Injection_type},\n"
            f"  Enable_compress={self.Service.Enable_compress},\n"
            f"  Compress_factor={self.Service.Compress_factor},\n"
            f"  Enable_security={self.Service.Enable_security},\n"
            f"  Knowledge_block_num={self.Service.Knowledge_block_num},\n"
            f"  Knowledge_List={self.Service.Knowledge_List},\n"
            f"  Knowledge_length={self.Service.Knowledge_length},\n"
            f"  SLO_TTFT(ms)={self.Service.SLO_TTFT},\n"
            f"  SLO_E2E(ms)={self.Service.SLO_E2E},\n"
            f"  SLO_TPOT(ms)={self.Service.SLO_TPOT},\n"
            f"  Endpoint_type={self.Service.Endpoint_type},\n"
            f"--------Task--------\n"
            f"  User_addr={self.Task.User_addr},\n"
            f"  KDN_server_addr={self.Task.KDN_server_addr},\n"
            f"  default_know_addr={self.Task.default_know_addr},\n"
            f"  P_proxy_addr={self.Task.P_proxy_addr},\n"
            f"  D_proxy_addr={self.Task.D_proxy_addr},\n"
            f"  P_proxy_port={self.Task.P_proxy_port},\n"
            f"  D_proxy_port={self.Task.D_proxy_port},\n"
            f"  prefill_instance={self.Task.prefill_instance},\n"
            f"  decode_instance={self.Task.decode_instance},\n"
            f"  batch_order={self.Task.batch_order},\n"
            f"  User_url_path={self.Task.User_url_path},\n"
            f")"
        )
