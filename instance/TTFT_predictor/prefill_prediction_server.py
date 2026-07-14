# prefill_prediction_server.py
"""FastAPI service for online TTFT prediction and prefill measurement reporting."""

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import uvicorn
from typing import Optional

# Configure logging.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Module 1: import core prediction interfaces. ---
try:
    from prefill_predictor import predict_ttft, update_prefill_data, perform_detailed_warmup
except ImportError:
    print("[Critical Error] prefill_predictor not found.")
    exit(1)

# --- Helper: background warmup. ---
async def run_warmup_in_background():
    """
    Run the real warmup flow in the background.
    Delay a few seconds to ensure the server is fully started and can handle /report_prefill requests.
    """
    logger.info(">>> [Background] Waiting 5 seconds for the HTTP service to become ready...")
    await asyncio.sleep(5) 
    
    logger.info(">>> [Background] Starting real warmup...")
    try:
        # Tune repeats as needed.
        await perform_detailed_warmup(repeats=3)
        logger.info(">>> [Background] Real warmup complete; model has been calibrated.")
    except Exception as e:
        logger.error(f">>> [Background] Warmup failed: {e}", exc_info=True)


# --- Module 2: FastAPI lifespan management. ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server is starting... ")
    
    # 1. Trigger singleton initialization for fast cold start.
    # This is an in-memory operation and is fast enough to block on.
    try:
        await predict_ttft(batch_size=1, prompt_length=1)
        logger.info("Base model instance is ready (dummy init).")
    except Exception as e:
        logger.error(f"[Error] Dummy Init failed: {e}")

    # 2. Key change: start a background task for real warmup.
    # Do not await it; let it run in the background so lifespan can finish and the server can start serving.
    asyncio.create_task(run_warmup_in_background())
    logger.info("Background warmup task has been scheduled.")

    yield # Server starts running and handling requests, including /report_prefill.
    
    logger.info("Server is shutting down.")


# --- Module 3: create FastAPI app and API endpoints. ---
app = FastAPI(
    title="vLLM TTFT Predictor API",
    description="Online prediction service plus online data-collection regression",
    version="1.1.0",
    lifespan=lifespan
)

# ... Remaining data models and API endpoint code stay unchanged. ...
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

@app.get("/", summary="Health check endpoint")
async def read_root():
    return {"status": "ok", "message": "TTFT Predictor is running."}

@app.post("/predict", summary="Predict TTFT", response_model=PredictionResponse)
async def handle_prediction(request: PredictionRequest):
    prediction_sec = await predict_ttft(
        batch_size=request.batch_size, 
        prompt_length=request.prompt_length
    )
    return {
        "predicted_ttft_seconds": prediction_sec,
        "predicted_ttft_ms": prediction_sec * 1000
    }

@app.post("/report_prefill", summary="Receive actual Prefill duration to update the model")
async def handle_prefill_report(report: PrefillReportRequest, background_tasks: BackgroundTasks):
    logger.info(f"Received POST request to /report_prefill with batch_size={report.batch_size}, prompt_length={report.prompt_length}, prefill_time={report.prefill_time_seconds}")
    
    background_tasks.add_task(
        update_prefill_data,
        batch_size=report.batch_size if report.batch_size is not None else 1,
        prompt_length=report.prompt_length,
        prefill_time=report.prefill_time_seconds
    )
    return {"status": "received", "msg": "Data queued for model update"}

# --- Module 4: run server. ---
if __name__ == "__main__":
    logger.info("Starting uvicorn server...")
    # In production, running directly in code is usually discouraged; prefer command-line startup.
    # Kept here for convenience.
    uvicorn.run("prefill_prediction_server:app", host="172.18.0.250", port=9003, reload=True)
