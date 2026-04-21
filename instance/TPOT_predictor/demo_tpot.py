import asyncio
from tpot_predictor import (
    collect_continuous_tpot_curve,
    compare_tpot_between_scenarios,
    export_scenario_compare,
    summarize_results,
)

async def main():
    result = await collect_continuous_tpot_curve(
        batch_size=1,
        real_input_length=33,   # 固定起点
        length_start=33,        # 想看的连续 length 起点
        length_end=1024,         # 想看的连续 length 终点
        max_tokens=64,          # 单次请求覆盖窗口长度
        repeats=5,              # 重复次数，建议至少 3
        overlap_tokens=16,       # 相邻窗口重叠一点，减少缺口
        concurrency = 1,
        outlier_method = "mad",
        outlier_threshold = 3.5,
        min_samples_for_filter = 5,
        smooth_window = 5,
        spike_ratio_threshold = 1.8,
    )

    # 连续区间结果：按 64,65,...,128 排好
    regressor = result["regressor"]

    print(result["coverage"])
    print("observed:", result["observed_continuous_points"])
    print("interpolated:", result["interpolated_points"])
    print("fitted:", result["fitted_points"])

    regressor.export_lengthwise_curve(
        "output/tpot_curve_bs2.csv",
        rows=result["range_curve"]
    )

asyncio.run(main())