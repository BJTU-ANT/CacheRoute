# 构建任务和任务批次的信息结构
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
定义调度器-本地代理的交互数据结构体 -> Class Request，调度器通过调度策略构建request并发给本地Proxy，随后Proxy按需求执行对应动作完成选池推理
    
    Class Request:
      |  Request_ID:用户任务的唯一标识ID
      |__Request_type：请求类型，如request，control，update等
      |  
      |__Class Prompt: 记录用户问题的基本信息，如具体问题，模型类型，问题长度等
      |   |  model                      模型类型
      |   |  user_prompt                用户问题
      |   |  token_length               问题的token长度
      |   |  max_token                  最长生成token数
      |   |  stream                     是否启用流式传输
      |   |  temperature                采样温度，默认1.0
      |   |__top_p                      nucleus sampling 截断阈值，默认1.0
      |
      |__Class Service： 映射用户问题的服务需求，如是否支持PD分离，是否支持知识注入，TTFT，E2E和TPOT的SLO需求
      |   |  Enable_PD_Disaggregation   是否启用PD分离
      |   |  Enable_know_injection      是否启用知识注入
      |   |  Enable_compress            是否允许对KVCache压缩
      |   |  Compress_factor            压缩比例
      |   |  Enable_security            是否启用安全模式（预留接口）
      |   |  Knowledge_block_num        知识块数量
      |   |  Knowledge_List             问题所需知识清单
      |   |  Knowledge_length           知识注入长度
      |   |  SLO_TTFT                   首token生成时间
      |   |  SLO_E2E                    端到端时延
      |   |  SLO_TPOT                   每秒token生成
      |   |__Endpoint_type              转发给vLLM的请求类型，包括 chat/completions 或 completions，对话类或补全类
      |  
      |__Class Task： 记录调度相关的任务信息，如KDN知识服务器的IP地址，PD池代理的IP地址，端口号
          |  User_addr                  用户IP地址
          |  KDN_server_addr            KDN服务器地址
          |  default_know_addr          默认文本注入服务器地址
          |  P_proxy_addr               P池代理IP地址
          |  D_proxy_addr               D池代理IP地址
          |  P_proxy_port               P池代理端口号
          |  D_proxy_port               D池代理端口号
          |  prefill_instance           该请求分配的Prefill实例IP地址及端口
          |  decode_instance            该请求分配的Decode实例IP地址及端口
          |__batch_order                该请求在其所分配实例中的批次顺序
"""

# ========================================================================================================================
# ------------------------------------------------------Prompt------------------------------------------------------------
# ========================================================================================================================
@dataclass
class Prompt:
    """
        定义用户问题的基本信息
            model<任务模型，str>
            user_prompt<用户问题，str>
            token_length<问题的token长度>
            bs<任务组的batch_size，通常情况下默认1>
            max_token<任务支持生成的最大token数>
            stream<是否启用流式传输>
            temperature<采样温度，默认1.0>
            top_p<nucleus sampling 截断阈值，默认1.0>
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
            根据 user_prompt 自动计算 token_length并返回。
            输入：model，user_prompt
            输出：token_length
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
        定义问题服务的SLO基本信息，通过用户IP地址映射具体服务等级，支持映射模块出于安全、个性化等方面的扩展
            Enable_PD_Disaggregation<是否允许问题进行PD分离处理，默认为True>
            Enable_know_injection<是否允许调用远端知识，默认为True>
            Enable_compress<是都允许进行KVCache的压缩，默认为True>
            Compress_factor<KVCache的压缩比率，默认0.3，可选0.3,0.5和0.7>
            Enable_security<是否启用安全模式，默认false，为后续安全内容提供接口>
            Knowledge_block_num<任务注入知识块的top数量，默认为3>
            Knowledge_List<知识块列表，里面的元素是Knowledge_ID,表征一个具体的知识块>
            Knowledge_length<任务注入知识库的token长度，默认为0>
            SLO_TTFT<任务组的TTFT SLO需求，即问题开始推理至产生提一个token所需要的时间，默认2000ms>
            SLO_E2E<任务从开始Prefill到结束Decode所需的完整时间，ms>
            SLO_TPOT<任务组的TPOT SLO需求，即自回归推理阶段平均生成默认20ms>
            Endpoint_type<转发给vLLM的请求类型，包括 chat/completions 或 completions，对话类或补全类>
    """
    Enable_PD_Disaggregation: bool
    Enable_know_injection: bool
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
            通过用户的IP地址映射出具体的服务SLO和功能启用
            输入：user_addr
            输出：服务SLO，如TTFT、TPOT、E2E，以及启用功能，是否启用PD分离，是否允许压缩，是否开启安全
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
    ) -> Tuple[List[str], int]:
        """
            根据用户问题，从知识库中检索出最相关的知识清单。

            参数：
                user_prompt：str ->用户问句，用于生成查询 embedding
                knowledge_block_num：int ->希望返回的知识块数量（top-k）
                embedder： EmbeddingModel ->外部注入的embedding模型
                knowledge_table: KnowledgeTable ->实例，内部已经构建好 FAISS 索引

            输出：
                knowledge_ids: 检索到的知识块 ID 列表（长度 <= knowledge_block_num）
                total_knowledge_length: 这些知识块 length 字段的总和
        """
        user_prompt = (user_prompt or "").strip()
        if not user_prompt:
            # 空问题，直接返回空列表
            return [], 0

        if knowledge_block_num <= 0:
            return [], 0

        print(f"[Knowledge retriever]: use embedder={DEFAULT_EMBED_MODEL}")

        # 1) 将 user_prompt 编码为 embedding
        query_embedding = embedder.encode_vector([user_prompt])[0]

        # 2) 在知识库中检索 top-k 知识块（内部优先用 FAISS，失败则回退 Python 扫描）
        results = knowledge_table.search_by_embedding(
            query_embedding=query_embedding,
            top_k=knowledge_block_num,
            min_score=float(SCHEDULER_RETRIEVAL_MIN_SCORE),
            min_ratio=float(SCHEDULER_RETRIEVAL_MIN_RATIO),
        )

        # 3) 提取 ID 列表和总长度
        knowledge_ids: List[str] = []
        total_len = 0
        for kid, unit, score in results:
            knowledge_ids.append(kid)
            # length 字段在你构建 knowledge_base.yaml 时已经写好
            total_len += int(getattr(unit, "length", 0))

        # 简单 debug
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


# ========================================================================================================================
# ------------------------------------------------------Task--------------------------------------------------------------
# ========================================================================================================================
@dataclass
class Task:
    """
        定义任务调度基本信息
            User_addr：用户的IP地址，表征用户身份
            KDN_server_addr<任务根据知识需求挑选出的最合适的KDN服务器地址>
            default_know_addr<默认的知识注入服务器，采用文本的注入方式>
            P_proxy_addr<处理任务的P池代理IP地址，默认为本地换回地址>
            P_proxy_port<处理任务的P池代理端口号，默认为8001>
            D_proxy_addr<处理任务的D池代理IP地址，默认为本地换回地址>
            D_proxy_port<处理任务的D池代理端口号，默认为8001>
            prefill_instance<该请求分配的Prefill实例IP地址及端口>
            decode_instance<该请求分配的Decode实例IP地址及端口>
            batch_order<该请求在其所分配实例中的批次顺序>
    """
    User_addr: str
    KDN_server_addr: str
    default_know_addr: str
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
        主结构体，用于描述任务的所有需求
            Request_ID：用户任务的唯一标识ID
            Request_type：请求类型，如request，control，update等
            Prompt<用于处理用户内容的相关信息>
            Service<用于处理用户服务的相关信息>
            Task<用于记录调度任务时的相关信息>
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
            strategy: Optional[Any] = None,
    ) -> "Request":
        """
            将原始用户请求信息payload，以及转换为完整的 Request 对象。
            url_path:用户发送的url地址，例如 http://127.0.0.1:7001/v1/chat/completions
            payload兼容的格式：
                1）旧版TCP协议纯payload：{"model": "...", "user_prompt": "..."}
                2）/v1/chat/completions 风格：{
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
                3）/v1/completions 风格：{
                    "model": "...",
                    "prompt": "...." 或 ["...","..."],
                    "max_tokens": 1024,
                    "temperature": 0.7,
                    "top_p": 0.95,
                    "stream": false,
                    ...}
            user_addr：调度器从 TCP / HTTP 层获取的客户端 IP
            request_id：调度器分配的请求 ID（可保持默认 0，然后调度器后续覆盖）
        """

        # print("[BuildRequest] Receive user request payload:", payload)
        model = payload.get("model")
        if not model:
            raise ValueError("Request payload 缺少必需字段 'model'")

        #----------------- 为兼容多种格式，设置user_prompt判断提取 -----------------------
        user_prompt: Optional[str] = None
        t0 = time.perf_counter()
        # Type A: 旧版协议，直接基于TCP给user_prompt
        if "user_prompt" in payload:
            user_prompt = str(payload["user_prompt"])

        # Type B: chat/completions 风格，使用 messages
        elif "messages" in payload:
            msgs = payload.get("messages") or []
            if not isinstance(msgs, list):
                raise ValueError("'messages' 字段必须是列表")

            last_user: Optional[str] = None
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                role = m.get("role", "")
                content = m.get("content", "")
                if role == "user":
                    # 不断覆盖，最终保留“最后一条 user 消息”
                    last_user = str(content)

            if last_user is not None:
                user_prompt = last_user
            else:
                # 没有 user 角色时，退化为把所有消息拼起来
                pieces: List[str] = []
                for m in msgs:
                    if not isinstance(m, dict):
                        continue
                    r = m.get("role", "")
                    c = m.get("content", "")
                    pieces.append(f"{r}: {c}")
                if pieces:
                    user_prompt = "\n".join(pieces)

        # Type C: completions 风格，使用 prompt
        elif "prompt" in payload:
            prompt = payload.get("prompt")
            if isinstance(prompt, str):
                user_prompt = prompt
            elif isinstance(prompt, list) and prompt:
                # 这里简单取第一个元素，你后面可以根据需要改成拼接
                user_prompt = str(prompt[0])

        if not user_prompt:
            raise ValueError("无法从请求中解析出 user_prompt（缺少 user_prompt/messages/prompt）")

        # ---- 通用请求参数：max_tokens / stream / temperature / top_p ----
        # 兼容没传的情况，给默认值
        max_tokens = int(payload.get("max_tokens", 1000) or 1000)
        stream = parse_stream_flag(payload.get("stream"))
        rag = parse_stream_flag(payload.get("RAG"))
        try:
            temperature = float(payload.get("temperature", 1.0))
        except (TypeError, ValueError):
            temperature = 1.0
        try:
            top_p = float(payload.get("top_p", 1.0))
        except (TypeError, ValueError):
            top_p = 1.0
        # 做一下简单夹紧，避免写乱
        if temperature <= 0:
            temperature = 1.0
        if not (0 < top_p <= 1.0):
            top_p = 1.0

        # ---- Request_type，目前统一标记为 "request"，后续可扩展 control/update ----
        request_type = payload.get("Request_type", "request")

        print("=============================\n"
              "   Start to build request.\n"
              "=============================")
        # print("[Request] get request ID:", request_id)
        # print("[Request] model:", model)
        # print("[Request] user_prompt:", user_prompt)

        # =========================
        # ---- 构造 Prompt 对象 ----
        # =========================
        # 构造prompt主要是
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
        # ----  构造 Service 对象 ----
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
            Endpoint_type="",
        )

        # 确认会话类型
        if "messages" in payload:
            service_obj.Endpoint_type = "chat/completions"
        else:
            service_obj.Endpoint_type = "completions"

        # 知识检索：填充 Knowledge_List / Knowledge_length
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
                )

                service_obj.Knowledge_List = knowledge_ids
                service_obj.Knowledge_length = total_len
            except Exception as e:
                print(f"[Service] knowledge_retriever failed: {e}")
                # 出错时维持默认值
                service_obj.Knowledge_List = []
                service_obj.Knowledge_length = 0
        else:
            # 未开启知识注入或未初始化知识库，保持默认值
            print("[Service] Ban knowledge task, skip retriever")
            service_obj.Knowledge_List = []
            service_obj.Knowledge_length = 0


        # ==========================
        # ---- 8) 构造 Task 对象 ----
        # ==========================
        task_obj = Task(
            User_addr=user_addr,
            KDN_server_addr = "",
            default_know_addr = "",  # 这里暂时用 KDN 作为默认知识服务器，可按需调整
            P_proxy_addr = "127.0.0.1",
            P_proxy_port = 8001,
            D_proxy_addr = "",
            D_proxy_port = 0,
            prefill_instance = "127.0.0.1",
            decode_instance = "",
            batch_order = 0,
            User_url_path=url_path
        )
        p_addr, p_port = "127.0.0.1", 8001
        # 若 scheduler 提供了策略和 proxy 列表，则在 build_request 内做选择
        try:
            if proxies and hasattr(strategy, "select"):
                # 统一策略接口：要求策略提供 select(proxies, request_obj_like) 或 select(proxies, payload)
                # 这里为了避免 build_request 依赖 scheduler 的 Request 类型，我们先把最小上下文传给策略：
                chosen = strategy.select(proxies=proxies, payload=payload, url_path=url_path, user_addr=user_addr)
                if chosen:
                    p_addr, p_port = chosen["host"], int(chosen["port"])
        except Exception as e:
            # 不要让调度失败影响原功能：失败就继续用 8001
            print(f"[Scheduler-Strategy]: select failed, fallback to default. err={e}")

        task_obj.P_proxy_addr, task_obj.P_proxy_port = p_addr, p_port

        # =========================
        # ---- 拼装最终 Request ----
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
            将Request对象序列化为可以通过HTTP发送的payload。
                默认使用 dataclasses 保留完整结构：
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

