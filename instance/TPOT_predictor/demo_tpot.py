"""Demo script for collecting a continuous TPOT curve for one batch size."""
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
        real_input_length=33,   # Fixed start point
        length_start=33,        # Start of the continuous length range to inspect
        length_end=1024,         # End of the continuous length range to inspect
        max_tokens=64,          # Window length covered by one request
        repeats=5,              # Repeat count; at least 3 is recommended
        overlap_tokens=16,       # Slightly overlap adjacent windows to reduce gaps
        concurrency = 1,
        outlier_method = "mad",
        outlier_threshold = 3.5,
        min_samples_for_filter = 5,
        smooth_window = 5,
        spike_ratio_threshold = 1.8,
    )

    # Continuous-range results sorted as 64, 65, ..., 128.
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