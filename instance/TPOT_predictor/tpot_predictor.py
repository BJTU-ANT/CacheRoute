import asyncio
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from transformers import AutoTokenizer

from request_generator import generate_prompt_with_tokens
from tpot_regressor import TPOTRegressor

VLLM_CONFIG_DEFAULT = {
    "host": "0.0.0.0",
    "port": 8000,
    "model_id": "llama3-70b",
    "tokenizer_path": "/workspace/llm-stack/models/LLM-Research/Meta-Llama-3-70B-Instruct/",
}

BATCH_SIZES_TO_TEST = range(1, 9)
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


async def get_regressor(
    outlier_method: str = "mad",
    outlier_threshold: float = 3.5,
    min_samples_for_filter: int = 5,
) -> TPOTRegressor:
    global _regressor
    async with _lock:
        if _regressor is None:
            print("[TPOT Predictor] Initializing collector...")
            _regressor = TPOTRegressor(
                outlier_method=outlier_method,
                outlier_threshold=outlier_threshold,
                min_samples_for_filter=min_samples_for_filter,
            )
        else:
            _regressor.set_outlier_config(outlier_method, outlier_threshold, min_samples_for_filter)
    return _regressor


async def collect_tpot_matrix(
    configs: List[Tuple[int, int]],
    vllm_config: Dict[str, Any] = VLLM_CONFIG_DEFAULT,
    max_tokens: int = 16,
    repeats: int = 3,
    concurrency: Optional[int] = None,
    outlier_method: str = "mad",
    outlier_threshold: float = 3.5,
    min_samples_for_filter: int = 5,
):
    regressor = await get_regressor(outlier_method, outlier_threshold, min_samples_for_filter)
    regressor.clear_data()
    await regressor.trigger_benchmark_requests(
        test_configs=configs,
        vllm_config=vllm_config,
        max_tokens=max_tokens,
        repeats_per_config=repeats,
        concurrency=concurrency,
    )
    return regressor


def _estimate_offset(tokenizer, target_prompt_length: int = 64) -> int:
    prompt = generate_prompt_with_tokens(tokenizer, target_prompt_length)
    chat_tokens = tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}],
        tokenize=True,
        add_generation_prompt=True,
    )
    return len(chat_tokens) - target_prompt_length


def _build_configs_for_sequence_range(
    batch_sizes: List[int],
    length_start: int,
    length_end: int,
    max_tokens: int,
    tokenizer,
) -> List[Tuple[int, int]]:
    configs: List[Tuple[int, int]] = []
    for bs in batch_sizes:
        offset = _estimate_offset(tokenizer)
        min_real_input = length_start
        max_real_input = max(length_start, length_end - max_tokens + 1)
        step = max(1, max_tokens // 2)
        n_steps = max(1, math.ceil((max_real_input - min_real_input + 1) / step))

        for i in range(n_steps):
            real_input = min_real_input + i * step
            target_pl = max(1, real_input - offset)
            configs.append((bs, target_pl))

    # 去重并排序
    uniq = sorted(set(configs), key=lambda x: (x[0], x[1]))
    return uniq


async def collect_tpot_range(
    batch_sizes: List[int],
    length_start: int,
    length_end: int,
    vllm_config: Dict[str, Any] = VLLM_CONFIG_DEFAULT,
    max_tokens: int = 16,
    repeats: int = 3,
    concurrency: Optional[int] = None,
    prefer_fitted: bool = True,
    fit_after_collect: bool = True,
    fit_label_key: str = "filtered_mean_tpot_ms",
    outlier_method: str = "mad",
    outlier_threshold: float = 3.5,
    min_samples_for_filter: int = 5,
):
    """
    面向真实 sequence_length 区间 [length_start, length_end] 的接口。

    对用户暴露的 length 语义：真实 sequence_length，而不是 target_prompt_length。
    内部会自动把区间映射为一组待测 target_prompt_length。
    """
    tokenizer = AutoTokenizer.from_pretrained(vllm_config["tokenizer_path"])
    configs = _build_configs_for_sequence_range(
        batch_sizes=batch_sizes,
        length_start=length_start,
        length_end=length_end,
        max_tokens=max_tokens,
        tokenizer=tokenizer,
    )

    regressor = await collect_tpot_matrix(
        configs=configs,
        vllm_config=vllm_config,
        max_tokens=max_tokens,
        repeats=repeats,
        concurrency=concurrency,
        outlier_method=outlier_method,
        outlier_threshold=outlier_threshold,
        min_samples_for_filter=min_samples_for_filter,
    )

    coeffs = None
    if fit_after_collect:
        try:
            coeffs = regressor.fit_four_term_regressor(label_key=fit_label_key)
        except Exception as exc:
            print(f"[TPOT][WARN] fit_after_collect failed: {exc}")

    range_rows: List[Dict[str, Any]] = []
    for bs in batch_sizes:
        rows = regressor.build_length_range_curve(
            batch_size=bs,
            length_start=length_start,
            length_end=length_end,
            prefer_fitted=prefer_fitted,
            label_key=fit_label_key,
        )
        range_rows.extend(rows)

    return {
        "requested": {
            "batch_sizes": batch_sizes,
            "length_range": [length_start, length_end],
            "length_semantics": "real sequence_length",
        },
        "planned_test_configs": configs,
        "fit_coefficients": coeffs,
        "range_curve": range_rows,
        "summary": regressor.build_summary(),
        "regressor": regressor,
    }


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


def summarize_results(
    summary: Dict[str, Any],
    full_curve_bs: Optional[int] = None,
    length_range: Optional[Tuple[int, int]] = None,
) -> str:
    lines = ["\n=== TPOT Benchmark Summary ===", "[Config-Level]"]
    for cfg in summary.get("configs", []):
        lines.append(
            "BS={batch_size}, target_PL={target_prompt_length}, tasks={tasks}, avg_ttft={avg_ttft_ms:.2f}ms, "
            "avg_offset={avg_input_length_offset:.2f}, min/max_real_input_length={min_real_input_length}/{max_real_input_length}".format(
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
    r0, r1 = length_range if length_range else (None, None)
    for bs_curve in summary.get("length_wise_by_bs", []):
        bs = bs_curve.get("batch_size")
        curve = bs_curve.get("length_tpot_curve") or []
        min_l = bs_curve.get("min_observed_sequence_length")
        max_l = bs_curve.get("max_observed_sequence_length")
        lines.append(f"BS={bs}, points={len(curve)}, min_observed_length={min_l}, max_observed_length={max_l}")

        should_full = full_curve_bs is not None and bs == full_curve_bs
        selected = curve
        if r0 is not None and r1 is not None:
            selected = [p for p in selected if r0 <= p.get("sequence_length", -1) <= r1]

        for point in (selected if should_full else selected[:5]):
            lines.append(
                "  L={sequence_length}, raw_n={raw_samples}, filt_n={filtered_samples}, "
                "raw_mean={raw_mean_tpot_ms:.3f}ms, filt_mean={filtered_mean_tpot_ms:.3f}ms, "
                "filt_median={filtered_median_tpot_ms:.3f}ms, filt_p95={filtered_p95_tpot_ms:.3f}ms, "
                "outliers={outlier_count}, source={value_source}".format(
                    **{
                        **point,
                        "raw_mean_tpot_ms": point.get("raw_mean_tpot_ms") or 0.0,
                        "filtered_mean_tpot_ms": point.get("filtered_mean_tpot_ms") or 0.0,
                        "filtered_median_tpot_ms": point.get("filtered_median_tpot_ms") or 0.0,
                        "filtered_p95_tpot_ms": point.get("filtered_p95_tpot_ms") or 0.0,
                    }
                )
            )

    lines.append("\n[Outlier Config]")
    lines.append(str(summary.get("outlier_config", {})))
    lines.append("\n[Note]")
    lines.append(summary.get("note", ""))
    return "\n".join(lines)


def fit_tpot_four_term(regressor: TPOTRegressor, label_key: str = "filtered_mean_tpot_ms") -> Dict[str, Any]:
    return regressor.fit_four_term_regressor(label_key=label_key)


def predict_decode_time(
    regressor: TPOTRegressor,
    batch_size: int,
    start_sequence_length: int,
    max_tokens: int,
    prefer_fitted: bool = True,
    label_key: str = "filtered_mean_tpot_ms",
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
