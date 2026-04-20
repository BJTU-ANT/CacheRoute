import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from transformers import AutoTokenizer

from request_generator import (
    bounded_gather,
    generate_prompt_with_tokens,
    send_stream_request_for_tpot,
)


@dataclass
class TPOTTaskRecord:
    request_id: str
    batch_size: int
    target_prompt_length: int
    real_input_length: int
    input_length_offset: int
    max_tokens: int
    ttft_seconds: float
    # 每个元素:
    # token_index: 第几个生成 token（从1开始）
    # sequence_length: 生成该 token 前的长度 = real_input_length + token_index - 1
    # tpot_seconds: 对应的时间增量
    token_steps: List[Dict[str, Any]]


class TPOTRegressor:
    """
    以测量/统计为主，不做参数回归。
    输出按 bs 与真实 sequence length 聚合的 TPOT 曲线。
    """

    def __init__(self):
        self._records: Dict[Tuple[int, int], List[TPOTTaskRecord]] = {}

    def clear_data(self):
        self._records = {}

    def add_record(self, record: TPOTTaskRecord):
        key = (record.batch_size, record.target_prompt_length)
        self._records.setdefault(key, []).append(record)

    @staticmethod
    def _compute_real_input_length(tokenizer, prompt: str) -> int:
        chat_tokens = tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            tokenize=True,
            add_generation_prompt=True,
        )
        return len(chat_tokens)

    @staticmethod
    def _percentile(values: List[float], q: float) -> Optional[float]:
        if not values:
            return None
        sorted_vals = sorted(values)
        if len(sorted_vals) == 1:
            return sorted_vals[0]
        idx = (len(sorted_vals) - 1) * q
        low = int(idx)
        high = min(low + 1, len(sorted_vals) - 1)
        frac = idx - low
        return sorted_vals[low] * (1 - frac) + sorted_vals[high] * frac

    async def trigger_benchmark_requests(
        self,
        test_configs: List[Tuple[int, int]],
        vllm_config: Dict[str, Any],
        max_tokens: int,
        repeats_per_config: int = 3,
        concurrency: Optional[int] = None,
    ):
        tokenizer = AutoTokenizer.from_pretrained(vllm_config["tokenizer_path"])
        timeout = aiohttp.ClientTimeout(total=900)
        request_counter = 0

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for bs, target_pl in test_configs:
                print(f"[TPOT] Start config BS={bs}, target_PL={target_pl}, repeats={repeats_per_config}")

                for r in range(repeats_per_config):
                    prompts = [generate_prompt_with_tokens(tokenizer, target_pl) for _ in range(bs)]
                    real_input_lengths = [
                        self._compute_real_input_length(tokenizer, prompt)
                        for prompt in prompts
                    ]

                    for idx, real_len in enumerate(real_input_lengths, start=1):
                        diff = real_len - target_pl
                        print(
                            f"[TPOT][LEN-DEBUG] BS={bs}, repeat={r+1}, task={idx}/{bs}, "
                            f"target_prompt_length={target_pl}, real_input_length={real_len}, diff={diff}"
                        )

                    run_coros = [
                        send_stream_request_for_tpot(
                            session=session,
                            host=vllm_config["host"],
                            port=vllm_config["port"],
                            model=vllm_config["model_id"],
                            prompt=prompt,
                            max_tokens=max_tokens,
                            tokenizer=tokenizer,
                        )
                        for prompt in prompts
                    ]

                    start_round = time.perf_counter()
                    results = await bounded_gather(run_coros, concurrency=concurrency or bs)
                    round_ms = (time.perf_counter() - start_round) * 1000

                    success_count = 0
                    for task_idx, result in enumerate(results, start=1):
                        request_counter += 1
                        req_id = f"bs{bs}-tpl{target_pl}-r{r+1}-t{task_idx}-{request_counter}"
                        if not result.success or result.ttft_seconds is None:
                            print(f"[TPOT][WARN] req={req_id} failed, error={result.error}")
                            continue

                        success_count += 1
                        real_input_len = real_input_lengths[task_idx - 1]
                        token_steps = []
                        for step in result.token_steps:
                            token_steps.append(
                                {
                                    "token_index": step.token_index,
                                    "sequence_length": real_input_len + step.token_index - 1,
                                    "tpot_seconds": step.delta_seconds,
                                }
                            )

                        self.add_record(
                            TPOTTaskRecord(
                                request_id=req_id,
                                batch_size=bs,
                                target_prompt_length=target_pl,
                                real_input_length=real_input_len,
                                input_length_offset=real_input_len - target_pl,
                                max_tokens=max_tokens,
                                ttft_seconds=result.ttft_seconds,
                                token_steps=token_steps,
                            )
                        )

                    print(
                        f"[TPOT] Finished BS={bs}, target_PL={target_pl}, repeat={r+1}, "
                        f"success={success_count}/{bs}, elapsed={round_ms:.1f}ms"
                    )

    def build_summary(self) -> Dict[str, Any]:
        configs_summary: List[Dict[str, Any]] = []
        length_bucket_by_bs: Dict[int, Dict[int, List[float]]] = {}

        for (bs, target_pl), records in sorted(self._records.items(), key=lambda x: (x[0][0], x[0][1])):
            ttft_values = [rec.ttft_seconds for rec in records]
            offsets = [rec.input_length_offset for rec in records]

            config_data = {
                "batch_size": bs,
                "target_prompt_length": target_pl,
                "tasks": len(records),
                "avg_ttft_ms": (statistics.mean(ttft_values) * 1000) if ttft_values else None,
                "avg_input_length_offset": statistics.mean(offsets) if offsets else None,
                "min_input_length_offset": min(offsets) if offsets else None,
                "max_input_length_offset": max(offsets) if offsets else None,
            }
            configs_summary.append(config_data)

            if bs not in length_bucket_by_bs:
                length_bucket_by_bs[bs] = {}

            for rec in records:
                for step in rec.token_steps:
                    seq_len = step["sequence_length"]
                    length_bucket_by_bs[bs].setdefault(seq_len, []).append(step["tpot_seconds"])

        curves_by_bs: List[Dict[str, Any]] = []
        for bs, curve_map in sorted(length_bucket_by_bs.items(), key=lambda x: x[0]):
            points = []
            for seq_len, vals in sorted(curve_map.items(), key=lambda x: x[0]):
                mean_v = statistics.mean(vals)
                median_v = statistics.median(vals)
                p95_v = self._percentile(vals, 0.95)
                points.append(
                    {
                        "sequence_length": seq_len,
                        "samples": len(vals),
                        "mean_tpot_ms": mean_v * 1000,
                        "median_tpot_ms": median_v * 1000,
                        "p95_tpot_ms": (p95_v * 1000) if p95_v is not None else None,
                    }
                )

            curves_by_bs.append(
                {
                    "batch_size": bs,
                    "length_tpot_curve": points,
                }
            )

        return {
            "configs": configs_summary,
            "length_wise_by_bs": curves_by_bs,
        }

    def export_json(self, output_path: str):
        payload = {
            "summary": self.build_summary(),
            "records": [
                asdict(rec)
                for records in self._records.values()
                for rec in records
            ],
        }
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[TPOT] Exported benchmark result => {path}")
