# prefill_prediction_server.py

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import uvicorn
from typing import Optional

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 模块 1: 导入您的核心预测接口 ---
try:
    from prefill_predictor import predict_ttft, update_prefill_data, perform_detailed_warmup
except ImportError:
    print("[Critical Error] prefill_predictor not found.")
    exit(1)

# --- 辅助函数：后台预热 ---
async def run_warmup_in_background():
    """
    在后台运行真实的预热流程。
    延迟几秒执行，确保 Server 已经完全启动并能够处理 /report_prefill 请求。
    """
    logger.info(">>> [Background] 等待 5秒 确保 HTTP 服务完全就绪...")
    await asyncio.sleep(5) 
    
    logger.info(">>> [Background] 开始执行真实 Warmup...")
    try:
        # 这里的 repeats 可以根据需要调整
        await perform_detailed_warmup(repeats=3)
        logger.info(">>> [Background] 真实 Warmup 完成，模型已精确校准。")
    except Exception as e:
        logger.error(f">>> [Background] Warmup 失败: {e}", exc_info=True)


# --- 模块 2: FastAPI 的生命周期管理 ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("服务器正在启动... ")
    
    # 1. 触发单例初始化 (快速冷启动)
    # 这个是纯内存操作，很快，可以阻塞等待
    try:
        await predict_ttft(batch_size=1, prompt_length=1)
        logger.info("基础模型实例已就绪 (Dummy Init)。")
    except Exception as e:
        logger.error(f"[Error] Dummy Init failed: {e}")

    # 2. [关键修改] 启动后台任务进行真实 Warmup
    # 不要 await！让它在后台跑，这样 lifespan 可以结束，Server 才能开始服务。
    asyncio.create_task(run_warmup_in_background())
    logger.info("后台 Warmup 任务已调度。")

    yield # 服务器开始运行，处理请求（包括 /report_prefill）
    
    logger.info("服务器正在关闭。")


# --- 模块 3: 创建 FastAPI 应用和 API 端点 ---
app = FastAPI(
    title="vLLM TTFT Predictor API",
    description="在线预测服务 + 在线数据收集回归",
    version="1.1.0",
    lifespan=lifespan
)

# ... (其余的数据模型和 API 接口代码保持不变) ...
class PredictionRequest(BaseModel):
    batch_size: int
    prompt_length: int

class PredictionResponse(BaseModel):
    predicted_ttft_seconds: float
    predicted_ttft_ms: float
    
class PrefillReportRequest(BaseModel):
    batch_size: Optional[int] = None 
    prompt_length: int
    prefill_time_seconds: float

@app.get("/", summary="健康检查接口")
async def read_root():
    return {"status": "ok", "message": "TTFT Predictor is running."}

@app.post("/predict", summary="预测 TTFT", response_model=PredictionResponse)
async def handle_prediction(request: PredictionRequest):
    prediction_sec = await predict_ttft(
        batch_size=request.batch_size, 
        prompt_length=request.prompt_length
    )
    return {
        "predicted_ttft_seconds": prediction_sec,
        "predicted_ttft_ms": prediction_sec * 1000
    }

@app.post("/report_prefill", summary="接收实际 Prefill 耗时以更新模型")
async def handle_prefill_report(report: PrefillReportRequest, background_tasks: BackgroundTasks):
    logger.info(f"Received POST request to /report_prefill with batch_size={report.batch_size}, prompt_length={report.prompt_length}, prefill_time={report.prefill_time_seconds}")
    
    background_tasks.add_task(
        update_prefill_data,
        batch_size=report.batch_size if report.batch_size is not None else 1,
        prompt_length=report.prompt_length,
        prefill_time=report.prefill_time_seconds
    )
    return {"status": "received", "msg": "Data queued for model update"}

# --- 模块 4: 运行服务器 ---
if __name__ == "__main__":
    logger.info("正在启动 uvicorn 服务器...")
    # 注意：在生产环境中通常不建议在代码里直接 run，而是用命令行启动
    # 这里为了方便保留
    uvicorn.run("prefill_prediction_server:app", host="172.18.0.250", port=9003, reload=True)
