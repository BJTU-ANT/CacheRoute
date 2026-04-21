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
    smooth_window: int = 5,
    spike_ratio_threshold: float = 1.8,
) -> TPOTRegressor:
    global _regressor
    async with _lock:
        if _regressor is None:
            print("[TPOT Predictor] Initializing collector...")
            _regressor = TPOTRegressor(
                outlier_method=outlier_method,
                outlier_threshold=outlier_threshold,
                min_samples_for_filter=min_samples_for_filter,
                smooth_window=smooth_window,
                spike_ratio_threshold=spike_ratio_threshold,
            )
        else:
            _regressor.set_outlier_config(
                outlier_method,
                outlier_threshold,
                min_samples_for_filter,
                smooth_window,
                spike_ratio_threshold,
            )
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
    smooth_window: int = 5,
    spike_ratio_threshold: float = 1.8,
    with_prefill_load: bool = False,
    prefill_prompt_length: int = 1024,
    prefill_concurrency: int = 1,
    prefill_interval_ms: int = 0,
    prefill_max_tokens: int = 1,
    prefill_injection_mode: str = "before_request",
):
    regressor = await get_regressor(
        outlier_method=outlier_method,
        outlier_threshold=outlier_threshold,
        min_samples_for_filter=min_samples_for_filter,
        smooth_window=smooth_window,
        spike_ratio_threshold=spike_ratio_threshold,
    )
    regressor.clear_data()
    await regressor.trigger_benchmark_requests(
        test_configs=configs,
        vllm_config=vllm_config,
        max_tokens=max_tokens,
        repeats_per_config=repeats,
        concurrency=concurrency,
        scenario="with_prefill_load" if with_prefill_load else "baseline",
        prefill_load_config={
            "prefill_prompt_length": prefill_prompt_length,
            "prefill_concurrency": prefill_concurrency,
            "prefill_interval_ms": prefill_interval_ms,
            "prefill_max_tokens": prefill_max_tokens,
        } if with_prefill_load else None,
        prefill_injection_mode=prefill_injection_mode,
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


def _build_continuous_configs_for_single_bs(
    batch_size: int,
    length_start: int,
    length_end: int,
    max_tokens: int,
    tokenizer,
    real_input_length: Optional[int] = None,
    overlap_tokens: int = 4,
) -> List[Tuple[int, int]]:
    """
    为单个 bs 规划“连续长度采样模式”：
    通过若干 real_input_length 起点 + 固定 max_tokens，尽量覆盖 [length_start, length_end]。
    """
    offset = _estimate_offset(tokenizer)
    window = max(1, int(max_tokens))
    stride = max(1, window - max(0, int(overlap_tokens)))

    starts: List[int] = []
    if real_input_length is not None:
        starts.append(int(real_input_length))
    if length_start not in starts:
        starts.append(length_start)

    s = min(starts)
    while s <= length_end:
        starts.append(s)
        s += stride

    starts = sorted(set(starts))
    configs: List[Tuple[int, int]] = []
    for real_start in starts:
        # 这个请求观测窗口是 [real_start, real_start + max_tokens - 1]
        if real_start > length_end:
            continue
        if real_start + window - 1 < length_start:
            continue
        target_pl = max(1, real_start - offset)
        configs.append((batch_size, target_pl))
    return sorted(set(configs), key=lambda x: (x[0], x[1]))


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
    fit_label_key: str = "default_tpot_ms",
    outlier_method: str = "mad",
    outlier_threshold: float = 3.5,
    min_samples_for_filter: int = 5,
    smooth_window: int = 5,
    spike_ratio_threshold: float = 1.8,
    with_prefill_load: bool = False,
    prefill_prompt_length: int = 1024,
    prefill_concurrency: int = 1,
    prefill_interval_ms: int = 0,
    prefill_max_tokens: int = 1,
    prefill_injection_mode: str = "before_request",
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
        smooth_window=smooth_window,
        spike_ratio_threshold=spike_ratio_threshold,
        with_prefill_load=with_prefill_load,
        prefill_prompt_length=prefill_prompt_length,
        prefill_concurrency=prefill_concurrency,
        prefill_interval_ms=prefill_interval_ms,
        prefill_max_tokens=prefill_max_tokens,
        prefill_injection_mode=prefill_injection_mode,
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
            scenario="with_prefill_load" if with_prefill_load else "baseline",
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


async def collect_continuous_tpot_curve(
    batch_size: int,
    real_input_length: int,
    length_start: int,
    length_end: int,
    vllm_config: Dict[str, Any] = VLLM_CONFIG_DEFAULT,
    max_tokens: int = 32,
    repeats: int = 3,
    concurrency: Optional[int] = None,
    overlap_tokens: int = 4,
    prefer_fitted: bool = True,
    fit_after_collect: bool = True,
    fit_label_key: str = "default_tpot_ms",
    outlier_method: str = "mad",
    outlier_threshold: float = 3.5,
    min_samples_for_filter: int = 5,
    smooth_window: int = 5,
    spike_ratio_threshold: float = 1.8,
    with_prefill_load: bool = False,
    prefill_prompt_length: int = 1024,
    prefill_concurrency: int = 1,
    prefill_interval_ms: int = 0,
    prefill_max_tokens: int = 1,
    prefill_injection_mode: str = "before_request",
):
    """
    连续长度采样主模式（单 bs）：
    用 real_input_length 起点 + max_tokens 窗口覆盖 [length_start, length_end]。
    """
    tokenizer = AutoTokenizer.from_pretrained(vllm_config["tokenizer_path"])
    configs = _build_continuous_configs_for_single_bs(
        batch_size=batch_size,
        length_start=length_start,
        length_end=length_end,
        max_tokens=max_tokens,
        tokenizer=tokenizer,
        real_input_length=real_input_length,
        overlap_tokens=overlap_tokens,
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
        smooth_window=smooth_window,
        spike_ratio_threshold=spike_ratio_threshold,
        with_prefill_load=with_prefill_load,
        prefill_prompt_length=prefill_prompt_length,
        prefill_concurrency=prefill_concurrency,
        prefill_interval_ms=prefill_interval_ms,
        prefill_max_tokens=prefill_max_tokens,
        prefill_injection_mode=prefill_injection_mode,
    )

    coeffs = None
    if fit_after_collect:
        try:
            coeffs = regressor.fit_four_term_regressor(label_key=fit_label_key)
        except Exception as exc:
            print(f"[TPOT][WARN] fit_after_collect failed: {exc}")

    range_rows = regressor.build_length_range_curve(
        batch_size=batch_size,
        length_start=length_start,
        length_end=length_end,
        prefer_fitted=prefer_fitted,
        label_key=fit_label_key,
        scenario="with_prefill_load" if with_prefill_load else "baseline",
    )
    coverage = regressor.check_length_coverage(
        batch_size, length_start, length_end,
        scenario="with_prefill_load" if with_prefill_load else "baseline",
    )
    observed = [r["sequence_length"] for r in range_rows if r.get("value_source") == "observed"]
    interpolated = [r["sequence_length"] for r in range_rows if r.get("value_source") == "interpolated"]
    fitted = [r["sequence_length"] for r in range_rows if r.get("value_source") == "fitted"]

    return {
        "requested": {
            "batch_size": batch_size,
            "real_input_length": real_input_length,
            "length_range": [length_start, length_end],
            "length_semantics": "real sequence_length",
            "scenario": "with_prefill_load" if with_prefill_load else "baseline",
        },
        "planned_test_configs": configs,
        "fit_coefficients": coeffs,
        "range_curve": range_rows,
        "coverage": coverage,
        "observed_continuous_points": observed,
        "interpolated_points": interpolated,
        "fitted_points": fitted,
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
        scenario = bs_curve.get("scenario", "baseline")
        bs = bs_curve.get("batch_size")
        curve = bs_curve.get("length_tpot_curve") or []
        min_l = bs_curve.get("min_observed_sequence_length")
        max_l = bs_curve.get("max_observed_sequence_length")
        lines.append(f"Scenario={scenario}, BS={bs}, points={len(curve)}, min_observed_length={min_l}, max_observed_length={max_l}")

        should_full = full_curve_bs is not None and bs == full_curve_bs
        selected = curve
        if r0 is not None and r1 is not None:
            selected = [p for p in selected if r0 <= p.get("sequence_length", -1) <= r1]

        for point in (selected if should_full else selected[:5]):
            lines.append(
                "  L={sequence_length}, raw_n={raw_samples}, filt_n={filtered_samples}, "
                "outliers={outlier_count}, low_conf={is_low_confidence}, spike={suspicious_spike}, "
                "filt_median={filtered_median_tpot_ms:.3f}ms, smooth={smoothed_tpot_ms:.3f}ms, "
                "default={default_tpot_ms:.3f}ms, source={value_source}".format(
                    **{
                        **point,
                        "filtered_median_tpot_ms": point.get("filtered_median_tpot_ms") or 0.0,
                        "smoothed_tpot_ms": point.get("smoothed_tpot_ms") or 0.0,
                        "default_tpot_ms": point.get("default_tpot_ms") or 0.0,
                    }
                )
            )

    lines.append("\n[Outlier Config]")
    lines.append(str(summary.get("outlier_config", {})))
    lines.append("\n[Note]")
    lines.append(summary.get("note", ""))
    return "\n".join(lines)


def fit_tpot_four_term(regressor: TPOTRegressor, label_key: str = "default_tpot_ms") -> Dict[str, Any]:
    return regressor.fit_four_term_regressor(label_key=label_key)


def predict_decode_time(
    regressor: TPOTRegressor,
    batch_size: int,
    start_sequence_length: int,
    max_tokens: int,
    prefer_fitted: bool = True,
    label_key: str = "default_tpot_ms",
    scenario: str = "baseline",
) -> Dict[str, Any]:
    return regressor.predict_decode_time_ms(
        batch_size=batch_size,
        start_sequence_length=start_sequence_length,
        max_tokens=max_tokens,
        prefer_fitted=prefer_fitted,
        label_key=label_key,
        scenario=scenario,
    )


def check_length_coverage(
    regressor: TPOTRegressor,
    batch_size: int,
    length_start: int,
    length_end: int,
    scenario: str = "baseline",
) -> Dict[str, Any]:
    return regressor.check_length_coverage(
        batch_size=batch_size,
        length_start=length_start,
        length_end=length_end,
        scenario=scenario,
    )


def compare_tpot_between_scenarios(
    regressor: TPOTRegressor,
    batch_size: int,
    length_start: int,
    length_end: int,
    baseline_scenario: str = "baseline",
    prefill_scenario: str = "with_prefill_load",
    value_key: str = "default_tpot_ms",
    prefer_fitted: bool = True,
) -> List[Dict[str, Any]]:
    return regressor.compare_tpot_between_scenarios(
        batch_size=batch_size,
        length_start=length_start,
        length_end=length_end,
        baseline_scenario=baseline_scenario,
        prefill_scenario=prefill_scenario,
        value_key=value_key,
        prefer_fitted=prefer_fitted,
    )


def compare_pre_post_injection_tpot(
    regressor: TPOTRegressor,
    batch_size: int,
    length_range: Tuple[int, int],
    scenario: str = "with_prefill_load",
) -> Dict[str, Any]:
    return regressor.compare_pre_post_injection_tpot(
        batch_size=batch_size,
        length_range=length_range,
        scenario=scenario,
    )


def export_scenario_compare(
    regressor: TPOTRegressor,
    output_path: str,
    rows: List[Dict[str, Any]],
):
    regressor.export_compare_results(output_path=output_path, rows=rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def _main():
        summary = await run_default_benchmark(max_tokens=16, repeats=2)
        print(summarize_results(summary))

    asyncio.run(_main())
