import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from tpot_regressor import TPOTRegressor

VLLM_CONFIG_DEFAULT = {
    "host": "0.0.0.0",
    "port": 8000,
    "model_id": "llama3-70b",
    "tokenizer_path": "/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct/",
}

BATCH_SIZES_TO_TEST = range(1, 9)
# 短区间加密，长区间放宽：优先覆盖更短 length
TOKEN_LENGTHS_TO_TEST = [
    *range(8, 128, 8),
    *range(128, 512, 32),
    *range(512, 2048, 64),
]

WARM_UP_CONFIGS_DEFAULT = [
    (bs, pl)
    for bs in BATCH_SIZES_TO_TEST
    for pl in TOKEN_LENGTHS_TO_TEST
    if bs * pl <= 10000
]

_regressor: Optional[TPOTRegressor] = None
_lock = asyncio.Lock()


async def get_regressor() -> TPOTRegressor:
    global _regressor
    async with _lock:
        if _regressor is None:
            print("[TPOT Predictor] Initializing collector...")
            _regressor = TPOTRegressor()
    return _regressor


async def collect_tpot_matrix(
    configs: List[Tuple[int, int]],
    vllm_config: Dict[str, Any] = VLLM_CONFIG_DEFAULT,
    max_tokens: int = 16,
    repeats: int = 3,
    concurrency: Optional[int] = None,
):
    regressor = await get_regressor()
    regressor.clear_data()
    await regressor.trigger_benchmark_requests(
        test_configs=configs,
        vllm_config=vllm_config,
        max_tokens=max_tokens,
        repeats_per_config=repeats,
        concurrency=concurrency,
    )
    return regressor


async def run_default_benchmark(
    max_tokens: int = 16,
    repeats: int = 3,
    output_path: str = "instance/TPOT_predictor/output/tpot_results.json",
    curve_output_path: str = "instance/TPOT_predictor/output/tpot_length_curve.csv",
):
    regressor = await collect_tpot_matrix(
        configs=WARM_UP_CONFIGS_DEFAULT,
        vllm_config=VLLM_CONFIG_DEFAULT,
        max_tokens=max_tokens,
        repeats=repeats,
    )
    regressor.export_json(output_path)
    regressor.export_lengthwise_curve(curve_output_path)
    return regressor.build_summary()


def summarize_results(summary: Dict[str, Any], full_curve_bs: Optional[int] = None) -> str:
    lines = ["\n=== TPOT Benchmark Summary ===", "[Config-Level]"]

    for cfg in summary.get("configs", []):
        lines.append(
            "BS={batch_size}, target_PL={target_prompt_length}, tasks={tasks}, "
            "avg_ttft={avg_ttft_ms:.2f}ms, avg_offset={avg_input_length_offset:.2f}, "
            "min/max_real_input_length={min_real_input_length}/{max_real_input_length}".format(
                **{
                    **cfg,
                    "avg_ttft_ms": cfg.get("avg_ttft_ms") or 0.0,
                    "avg_input_length_offset": cfg.get("avg_input_length_offset") or 0.0,
                    "min_real_input_length": cfg.get("min_real_input_length") or -1,
                    "max_real_input_length": cfg.get("max_real_input_length") or -1,
                }
            )
        )

    lines.append("\n[Length-wise Curve by BS]")
    for bs_curve in summary.get("length_wise_by_bs", []):
        bs = bs_curve.get("batch_size")
        curve = bs_curve.get("length_tpot_curve") or []
        min_l = bs_curve.get("min_observed_sequence_length")
        max_l = bs_curve.get("max_observed_sequence_length")
        lines.append(
            f"BS={bs}, curve_points={len(curve)}, min_observed_length={min_l}, max_observed_length={max_l}"
        )

        if full_curve_bs is not None and bs == full_curve_bs:
            for point in curve:
                lines.append(
                    "  L={sequence_length}, n={samples}, mean={mean_tpot_ms:.3f}ms, "
                    "median={median_tpot_ms:.3f}ms, p95={p95_tpot_ms:.3f}ms".format(
                        **{
                            **point,
                            "mean_tpot_ms": point.get("mean_tpot_ms") or 0.0,
                            "median_tpot_ms": point.get("median_tpot_ms") or 0.0,
                            "p95_tpot_ms": point.get("p95_tpot_ms") or 0.0,
                        }
                    )
                )
        else:
            for point in curve[:5]:
                lines.append(
                    "  L={sequence_length}, n={samples}, mean={mean_tpot_ms:.2f}ms, "
                    "median={median_tpot_ms:.2f}ms, p95={p95_tpot_ms:.2f}ms".format(
                        **{
                            **point,
                            "mean_tpot_ms": point.get("mean_tpot_ms") or 0.0,
                            "median_tpot_ms": point.get("median_tpot_ms") or 0.0,
                            "p95_tpot_ms": point.get("p95_tpot_ms") or 0.0,
                        }
                    )
                )

    lines.append("\n[Note]")
    lines.append(summary.get("note", ""))
    return "\n".join(lines)


def fit_tpot_four_term(regressor: TPOTRegressor, label_key: str = "mean_tpot_ms") -> Dict[str, Any]:
    return regressor.fit_four_term_regressor(label_key=label_key)


def predict_decode_time(
    regressor: TPOTRegressor,
    batch_size: int,
    start_sequence_length: int,
    max_tokens: int,
    prefer_fitted: bool = True,
    label_key: str = "mean_tpot_ms",
) -> Dict[str, Any]:
    return regressor.predict_decode_time_ms(
        batch_size=batch_size,
        start_sequence_length=start_sequence_length,
        max_tokens=max_tokens,
        prefer_fitted=prefer_fitted,
        label_key=label_key,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def _main():
        summary = await run_default_benchmark(max_tokens=16, repeats=2)
        print(summarize_results(summary))

    asyncio.run(_main())
