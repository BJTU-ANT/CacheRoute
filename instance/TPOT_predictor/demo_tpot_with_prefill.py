import asyncio
from tpot_predictor import (
    collect_continuous_tpot_curve,
    compare_tpot_between_scenarios,
    export_scenario_compare,
    summarize_results,
)

async def main():

    # 2) with prefill load
    loaded = await collect_continuous_tpot_curve(
        batch_size=1,
        real_input_length=33,
        length_start=33,
        length_end=128,
        max_tokens=32,
        repeats=3,
        overlap_tokens=8,
        concurrency=1,
        with_prefill_load=True,
        prefill_prompt_length=1024,
        prefill_concurrency=1,
        prefill_interval_ms=20,
        prefill_max_tokens=1,
    )

    # 注意：第二次 collect 会 clear_data，所以比较时用 loaded 这次返回的 regressor 不够
    # 因此建议分两步运行并分别导出 CSV，再用离线脚本比；或者改成单 regressor 持续累积。
    reg2 = loaded["regressor"]
    reg2.export_lengthwise_curve("output\prefill_bs1.csv", rows=loaded["range_curve"])
    print("=== WITH PREFILL LOAD ===")
    print(summarize_results(loaded["summary"], full_curve_bs=1, length_range=(33, 128)))
    print("prefill coverage:", loaded["coverage"])

asyncio.run(main())