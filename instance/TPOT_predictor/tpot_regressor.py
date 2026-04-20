import csv
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import numpy as np
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
    # token_index: 第几个生成 token（从1开始）
    # sequence_length: 生成该 token 前的长度 = real_input_length + token_index - 1
    # tpot_seconds: 对应时间增量
    token_steps: List[Dict[str, Any]]


class TPOTFourTermRegressor:
    """
    目标模型：
    TPOT(bs, length) = a1 * bs * length + a2 * bs + a3 * length + a4
    """

    def __init__(self):
        self._fitted = False
        self._coeffs = {"a1": 0.0, "a2": 0.0, "a3": 0.0, "a4": 0.0}
        self._label_key = "mean_tpot_ms"

    def fit(self, points: List[Dict[str, Any]], label_key: str = "mean_tpot_ms") -> Dict[str, Any]:
        self._label_key = label_key

        X_rows = []
        y_rows = []
        weights = []
        for p in points:
            y = p.get(label_key)
            if y is None:
                continue
            bs = float(p["batch_size"])
            length = float(p["sequence_length"])
            X_rows.append([bs * length, bs, length, 1.0])
            y_rows.append(float(y))
            weights.append(max(1.0, float(p.get("samples") or 1.0)))

        if len(X_rows) < 4:
            raise ValueError("Not enough length-wise points to fit four-term regressor.")

        X = np.asarray(X_rows, dtype=float)
        y = np.asarray(y_rows, dtype=float)
        w = np.sqrt(np.asarray(weights, dtype=float))
        Xw = X * w[:, None]
        yw = y * w

        coeffs, _, _, _ = np.linalg.lstsq(Xw, yw, rcond=None)
        self._coeffs = {
            "a1": float(coeffs[0]),
            "a2": float(coeffs[1]),
            "a3": float(coeffs[2]),
            "a4": float(coeffs[3]),
        }
        self._fitted = True
        return self.get_coefficients()

    def predict_tpot_ms(self, batch_size: int, sequence_length: int) -> float:
        if not self._fitted:
            raise RuntimeError("TPOTFourTermRegressor is not fitted.")
        a1, a2, a3, a4 = (
            self._coeffs["a1"],
            self._coeffs["a2"],
            self._coeffs["a3"],
            self._coeffs["a4"],
        )
        pred = a1 * batch_size * sequence_length + a2 * batch_size + a3 * sequence_length + a4
        return max(0.0, pred)

    def get_coefficients(self) -> Dict[str, Any]:
        return {
            "a1": self._coeffs["a1"],
            "a2": self._coeffs["a2"],
            "a3": self._coeffs["a3"],
            "a4": self._coeffs["a4"],
            "unit": "ms",
            "source": "TPOTFourTermRegressor",
            "label_key": self._label_key,
        }


class TPOTRegressor:
    """以测量/统计为主，输出按 bs 与真实 sequence_length 聚合的 TPOT 曲线。"""

    def __init__(self):
        self._records: Dict[Tuple[int, int], List[TPOTTaskRecord]] = {}
        self._four_term_regressor: Optional[TPOTFourTermRegressor] = None

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

    def get_lengthwise_points(self) -> List[Dict[str, Any]]:
        summary = self.build_summary()
        points: List[Dict[str, Any]] = []
        for by_bs in summary.get("length_wise_by_bs", []):
            bs = by_bs["batch_size"]
            for item in by_bs.get("length_tpot_curve", []):
                points.append(
                    {
                        "batch_size": bs,
                        "sequence_length": item["sequence_length"],
                        "samples": item["samples"],
                        "mean_tpot_ms": item["mean_tpot_ms"],
                        "median_tpot_ms": item["median_tpot_ms"],
                        "p95_tpot_ms": item["p95_tpot_ms"],
                    }
                )
        return points

    def fit_four_term_regressor(self, label_key: str = "mean_tpot_ms") -> Dict[str, Any]:
        points = self.get_lengthwise_points()
        model = TPOTFourTermRegressor()
        coeffs = model.fit(points=points, label_key=label_key)
        self._four_term_regressor = model
        return coeffs

    def _predict_from_curve_or_interp(self, batch_size: int, sequence_length: int, label_key: str) -> Optional[float]:
        points = [p for p in self.get_lengthwise_points() if p["batch_size"] == batch_size]
        if not points:
            return None

        exact = [p for p in points if p["sequence_length"] == sequence_length and p.get(label_key) is not None]
        if exact:
            return float(exact[0][label_key])

        sorted_pts = sorted([p for p in points if p.get(label_key) is not None], key=lambda x: x["sequence_length"])
        if not sorted_pts:
            return None

        left = None
        right = None
        for p in sorted_pts:
            if p["sequence_length"] < sequence_length:
                left = p
            elif p["sequence_length"] > sequence_length and right is None:
                right = p
                break

        if left and right:
            x0, y0 = left["sequence_length"], float(left[label_key])
            x1, y1 = right["sequence_length"], float(right[label_key])
            ratio = (sequence_length - x0) / (x1 - x0)
            return y0 + ratio * (y1 - y0)

        nearest = left or right
        return float(nearest[label_key]) if nearest else None

    def predict_decode_time_ms(
        self,
        batch_size: int,
        start_sequence_length: int,
        max_tokens: int,
        prefer_fitted: bool = True,
        label_key: str = "mean_tpot_ms",
    ) -> Dict[str, Any]:
        total_ms = 0.0
        source_counts = {"observed_or_interp": 0, "fitted": 0, "fallback_zero": 0}
        details = []

        for i in range(max_tokens):
            length_i = start_sequence_length + i
            value_ms = self._predict_from_curve_or_interp(batch_size, length_i, label_key)
            source = "observed_or_interp"

            if value_ms is None and prefer_fitted and self._four_term_regressor is not None:
                value_ms = self._four_term_regressor.predict_tpot_ms(batch_size, length_i)
                source = "fitted"

            if value_ms is None:
                value_ms = 0.0
                source = "fallback_zero"

            source_counts[source] += 1
            total_ms += value_ms
            details.append({"sequence_length": length_i, "tpot_ms": value_ms, "source": source})

        return {
            "batch_size": batch_size,
            "start_sequence_length": start_sequence_length,
            "max_tokens": max_tokens,
            "total_decode_ms": total_ms,
            "sources": source_counts,
            "steps": details,
        }

    def build_summary(self) -> Dict[str, Any]:
        configs_summary: List[Dict[str, Any]] = []
        length_bucket_by_bs: Dict[int, Dict[int, List[float]]] = {}

        for (bs, target_pl), records in sorted(self._records.items(), key=lambda x: (x[0][0], x[0][1])):
            ttft_values = [rec.ttft_seconds for rec in records]
            offsets = [rec.input_length_offset for rec in records]
            real_lengths = [rec.real_input_length for rec in records]

            config_data = {
                "batch_size": bs,
                "target_prompt_length": target_pl,
                "tasks": len(records),
                "avg_ttft_ms": (statistics.mean(ttft_values) * 1000) if ttft_values else None,
                "avg_input_length_offset": statistics.mean(offsets) if offsets else None,
                "min_input_length_offset": min(offsets) if offsets else None,
                "max_input_length_offset": max(offsets) if offsets else None,
                "min_real_input_length": min(real_lengths) if real_lengths else None,
                "max_real_input_length": max(real_lengths) if real_lengths else None,
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
                    "min_observed_sequence_length": points[0]["sequence_length"] if points else None,
                    "max_observed_sequence_length": points[-1]["sequence_length"] if points else None,
                    "length_tpot_curve": points,
                }
            )

        return {
            "configs": configs_summary,
            "length_wise_by_bs": curves_by_bs,
            "note": "sequence_length = real_input_length + token_index - 1; minimal observable length depends on chat template offset and sampled target prompt lengths.",
        }

    def export_lengthwise_curve(self, output_path: str):
        points = self.get_lengthwise_points()
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.suffix.lower() == ".csv":
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["batch_size", "sequence_length", "samples", "mean_tpot_ms", "median_tpot_ms", "p95_tpot_ms"],
                )
                writer.writeheader()
                for row in points:
                    writer.writerow(row)
        else:
            path.write_text(json.dumps(points, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"[TPOT] Exported length-wise curve => {path}")

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
