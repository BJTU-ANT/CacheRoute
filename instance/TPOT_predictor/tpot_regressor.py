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
    token_steps: List[Dict[str, Any]]


class TPOTFourTermRegressor:
    def __init__(self):
        self._fitted = False
        self._coeffs = {"a1": 0.0, "a2": 0.0, "a3": 0.0, "a4": 0.0}
        self._label_key = "filtered_median_tpot_ms"

    def fit(self, points: List[Dict[str, Any]], label_key: str = "filtered_median_tpot_ms") -> Dict[str, Any]:
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
            weights.append(max(1.0, float(p.get("filtered_samples") or p.get("raw_samples") or 1.0)))

        if len(X_rows) < 4:
            raise ValueError("Not enough points to fit TPOTFourTermRegressor.")

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
            raise RuntimeError("TPOTFourTermRegressor is not fitted")
        c = self._coeffs
        pred = c["a1"] * batch_size * sequence_length + c["a2"] * batch_size + c["a3"] * sequence_length + c["a4"]
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
    def __init__(
        self,
        outlier_method: str = "mad",
        outlier_threshold: float = 3.5,
        min_samples_for_filter: int = 5,
        smooth_window: int = 5,
        spike_ratio_threshold: float = 1.8,
    ):
        self._records: Dict[Tuple[int, int], List[TPOTTaskRecord]] = {}
        self._four_term_regressor: Optional[TPOTFourTermRegressor] = None
        self.set_outlier_config(
            outlier_method,
            outlier_threshold,
            min_samples_for_filter,
            smooth_window,
            spike_ratio_threshold,
        )

    def set_outlier_config(
        self,
        outlier_method: str,
        outlier_threshold: float,
        min_samples_for_filter: int,
        smooth_window: int = 5,
        spike_ratio_threshold: float = 1.8,
    ):
        self._outlier_method = str(outlier_method).lower()
        self._outlier_threshold = float(outlier_threshold)
        self._min_samples_for_filter = int(min_samples_for_filter)
        self._smooth_window = max(3, int(smooth_window))
        if self._smooth_window % 2 == 0:
            self._smooth_window += 1
        self._spike_ratio_threshold = float(spike_ratio_threshold)

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
        arr = sorted(values)
        if len(arr) == 1:
            return arr[0]
        idx = (len(arr) - 1) * q
        lo = int(idx)
        hi = min(lo + 1, len(arr) - 1)
        frac = idx - lo
        return arr[lo] * (1 - frac) + arr[hi] * frac

    def _filter_outliers(self, values: List[float]) -> Tuple[List[float], int]:
        if not values:
            return [], 0
        if self._outlier_method == "none" or len(values) < self._min_samples_for_filter:
            return list(values), 0

        arr = np.asarray(values, dtype=float)
        if self._outlier_method == "mad":
            median = np.median(arr)
            abs_dev = np.abs(arr - median)
            mad = np.median(abs_dev)
            if mad == 0:
                return list(values), 0
            robust_z = 0.6745 * (arr - median) / mad
            mask = np.abs(robust_z) <= self._outlier_threshold
        elif self._outlier_method == "iqr":
            q1 = np.percentile(arr, 25)
            q3 = np.percentile(arr, 75)
            iqr = q3 - q1
            if iqr == 0:
                return list(values), 0
            lower = q1 - self._outlier_threshold * iqr
            upper = q3 + self._outlier_threshold * iqr
            mask = (arr >= lower) & (arr <= upper)
        else:
            return list(values), 0

        filtered = arr[mask].tolist()
        if not filtered:
            return list(values), 0
        return filtered, len(values) - len(filtered)

    def _rolling_median(self, points: List[Dict[str, Any]], value_key: str) -> List[Optional[float]]:
        half = self._smooth_window // 2
        values = [p.get(value_key) for p in points]
        smoothed: List[Optional[float]] = []
        for i in range(len(values)):
            lo = max(0, i - half)
            hi = min(len(values), i + half + 1)
            window_vals = [v for v in values[lo:hi] if v is not None]
            if not window_vals:
                smoothed.append(None)
            else:
                smoothed.append(float(statistics.median(window_vals)))
        return smoothed

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
                    real_input_lengths = [self._compute_real_input_length(tokenizer, p) for p in prompts]

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
                        token_steps = [
                            {
                                "token_index": s.token_index,
                                "sequence_length": real_input_len + s.token_index - 1,
                                "tpot_seconds": s.delta_seconds,
                            }
                            for s in result.token_steps
                        ]
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
        bucket_by_bs_len: Dict[int, Dict[int, List[float]]] = {}

        for (bs, target_pl), records in sorted(self._records.items(), key=lambda x: (x[0][0], x[0][1])):
            ttft_values = [rec.ttft_seconds for rec in records]
            offsets = [rec.input_length_offset for rec in records]
            real_lengths = [rec.real_input_length for rec in records]
            configs_summary.append(
                {
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
            )

            bucket_by_bs_len.setdefault(bs, {})
            for rec in records:
                for step in rec.token_steps:
                    bucket_by_bs_len[bs].setdefault(step["sequence_length"], []).append(step["tpot_seconds"])

        curves_by_bs: List[Dict[str, Any]] = []
        for bs, m in sorted(bucket_by_bs_len.items(), key=lambda x: x[0]):
            points: List[Dict[str, Any]] = []
            for seq_len, vals in sorted(m.items(), key=lambda x: x[0]):
                raw_vals = list(vals)
                filtered_vals, outlier_count = self._filter_outliers(raw_vals)

                raw_mean = statistics.mean(raw_vals) * 1000 if raw_vals else None
                filtered_mean = statistics.mean(filtered_vals) * 1000 if filtered_vals else None
                filtered_median = statistics.median(filtered_vals) * 1000 if filtered_vals else None
                filtered_p95 = self._percentile(filtered_vals, 0.95)

                points.append(
                    {
                        "sequence_length": seq_len,
                        "raw_samples": len(raw_vals),
                        "filtered_samples": len(filtered_vals),
                        "raw_mean_tpot_ms": raw_mean,
                        "filtered_mean_tpot_ms": filtered_mean,
                        "filtered_median_tpot_ms": filtered_median,
                        "filtered_p95_tpot_ms": (filtered_p95 * 1000) if filtered_p95 is not None else None,
                        "outlier_count": outlier_count,
                        "is_low_confidence": len(filtered_vals) < self._min_samples_for_filter,
                        "suspicious_spike": False,
                        "smoothed_tpot_ms": None,
                        "default_tpot_ms": None,
                        "value_source": "observed",
                    }
                )

            # 同 bs 内滑动中位数平滑（用 filtered_median 优先）
            for p in points:
                p["_base_for_smooth"] = p.get("filtered_median_tpot_ms") if p.get("filtered_median_tpot_ms") is not None else p.get("filtered_mean_tpot_ms")
            smoothed = self._rolling_median(points, "_base_for_smooth")
            for i, s in enumerate(smoothed):
                points[i]["smoothed_tpot_ms"] = s

            # 邻域突增检测：低置信点 + 相邻长度比较
            for i, p in enumerate(points):
                if not p.get("is_low_confidence"):
                    continue
                cur = p.get("filtered_median_tpot_ms") or p.get("filtered_mean_tpot_ms")
                if cur is None:
                    continue
                neigh = []
                if i > 0:
                    left = points[i - 1].get("filtered_median_tpot_ms") or points[i - 1].get("filtered_mean_tpot_ms")
                    if left is not None:
                        neigh.append(left)
                if i + 1 < len(points):
                    right = points[i + 1].get("filtered_median_tpot_ms") or points[i + 1].get("filtered_mean_tpot_ms")
                    if right is not None:
                        neigh.append(right)
                if not neigh:
                    continue
                neigh_ref = float(statistics.median(neigh))
                if neigh_ref > 0 and cur > neigh_ref * self._spike_ratio_threshold:
                    p["suspicious_spike"] = True

            # default 值优先级：filtered_median -> smoothed -> filtered_mean
            for p in points:
                p["default_tpot_ms"] = (
                    p.get("filtered_median_tpot_ms")
                    if p.get("filtered_median_tpot_ms") is not None
                    else p.get("smoothed_tpot_ms")
                    if p.get("smoothed_tpot_ms") is not None
                    else p.get("filtered_mean_tpot_ms")
                )
                p.pop("_base_for_smooth", None)

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
            "outlier_config": {
                "outlier_method": self._outlier_method,
                "outlier_threshold": self._outlier_threshold,
                "min_samples_for_filter": self._min_samples_for_filter,
                "smooth_window": self._smooth_window,
                "spike_ratio_threshold": self._spike_ratio_threshold,
            },
            "default_value_for_fit_and_decode": "filtered_median_tpot_ms",
            "note": "TPOT step delta is measured using client-side stream arrival timestamps (time.perf_counter) between decoded token events.",
        }

    def get_lengthwise_points(self) -> List[Dict[str, Any]]:
        summary = self.build_summary()
        rows = []
        for by_bs in summary.get("length_wise_by_bs", []):
            bs = by_bs["batch_size"]
            for p in by_bs.get("length_tpot_curve", []):
                rows.append({"batch_size": bs, **p})
        return rows

    def check_length_coverage(self, batch_size: int, length_start: int, length_end: int) -> Dict[str, Any]:
        observed_lengths = sorted(
            {
                int(p["sequence_length"])
                for p in self.get_lengthwise_points()
                if int(p["batch_size"]) == int(batch_size)
                and length_start <= int(p["sequence_length"]) <= length_end
            }
        )
        expected = list(range(length_start, length_end + 1))
        observed_set = set(observed_lengths)
        missing = [x for x in expected if x not in observed_set]

        max_gap = 0
        cur_gap = 0
        for x in expected:
            if x in observed_set:
                max_gap = max(max_gap, cur_gap)
                cur_gap = 0
            else:
                cur_gap += 1
        max_gap = max(max_gap, cur_gap)

        ratio = (len(observed_lengths) / len(expected)) if expected else 0.0
        return {
            "batch_size": batch_size,
            "length_range": [length_start, length_end],
            "covered_lengths": observed_lengths,
            "missing_lengths": missing,
            "coverage_ratio": ratio,
            "max_gap": max_gap,
        }

    def fit_four_term_regressor(self, label_key: str = "filtered_median_tpot_ms") -> Dict[str, Any]:
        model = TPOTFourTermRegressor()
        coeffs = model.fit(self.get_lengthwise_points(), label_key=label_key)
        self._four_term_regressor = model
        return coeffs

    def _predict_from_curve_or_interp(self, batch_size: int, sequence_length: int, label_key: str) -> Tuple[Optional[float], str]:
        points = [p for p in self.get_lengthwise_points() if p["batch_size"] == batch_size and p.get(label_key) is not None]
        if not points:
            return None, "none"

        exact = [p for p in points if p["sequence_length"] == sequence_length]
        if exact:
            return float(exact[0][label_key]), "observed"

        sorted_pts = sorted(points, key=lambda x: x["sequence_length"])
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
            return y0 + ratio * (y1 - y0), "interpolated"

        nearest = left or right
        return (float(nearest[label_key]), "interpolated") if nearest else (None, "none")

    def build_length_range_curve(
        self,
        batch_size: int,
        length_start: int,
        length_end: int,
        prefer_fitted: bool = True,
        label_key: str = "default_tpot_ms",
    ) -> List[Dict[str, Any]]:
        rows = []
        for length in range(length_start, length_end + 1):
            value, source = self._predict_from_curve_or_interp(batch_size, length, label_key)
            if value is None and prefer_fitted and self._four_term_regressor is not None:
                value = self._four_term_regressor.predict_tpot_ms(batch_size, length)
                source = "fitted"
            if value is None:
                value = 0.0
                source = "none"

            rows.append(
                {
                    "batch_size": batch_size,
                    "sequence_length": length,
                    "raw_samples": 0,
                    "filtered_samples": 0,
                    "raw_mean_tpot_ms": None,
                    "filtered_mean_tpot_ms": None,
                    "filtered_median_tpot_ms": None,
                    "filtered_p95_tpot_ms": None,
                    "outlier_count": 0,
                    "is_low_confidence": True,
                    "suspicious_spike": False,
                    "smoothed_tpot_ms": None,
                    "default_tpot_ms": value,
                    "value_source": source,
                }
            )

        observed = {(r["batch_size"], r["sequence_length"]): r for r in self.get_lengthwise_points()}
        for i, row in enumerate(rows):
            k = (row["batch_size"], row["sequence_length"])
            if k in observed:
                merged = dict(observed[k])
                merged["batch_size"] = row["batch_size"]
                merged["value_source"] = "observed"
                rows[i] = merged
        return rows

    def predict_decode_time_ms(
        self,
        batch_size: int,
        start_sequence_length: int,
        max_tokens: int,
        prefer_fitted: bool = True,
        label_key: str = "default_tpot_ms",
    ) -> Dict[str, Any]:
        curve = self.build_length_range_curve(
            batch_size=batch_size,
            length_start=start_sequence_length,
            length_end=start_sequence_length + max_tokens - 1,
            prefer_fitted=prefer_fitted,
            label_key=label_key,
        )
        total_ms = sum(float(r.get(label_key) or 0.0) for r in curve)
        sources = {"observed": 0, "interpolated": 0, "fitted": 0, "none": 0}
        for r in curve:
            sources[r["value_source"]] = sources.get(r["value_source"], 0) + 1
        return {
            "batch_size": batch_size,
            "start_sequence_length": start_sequence_length,
            "max_tokens": max_tokens,
            "total_decode_ms": total_ms,
            "sources": sources,
            "steps": [{"sequence_length": r["sequence_length"], "tpot_ms": r.get(label_key), "source": r["value_source"]} for r in curve],
        }

    def export_lengthwise_curve(self, output_path: str, rows: Optional[List[Dict[str, Any]]] = None):
        rows = rows if rows is not None else self.get_lengthwise_points()
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "batch_size",
            "sequence_length",
            "raw_samples",
            "filtered_samples",
            "raw_mean_tpot_ms",
            "filtered_mean_tpot_ms",
            "filtered_median_tpot_ms",
            "filtered_p95_tpot_ms",
            "outlier_count",
            "is_low_confidence",
            "suspicious_spike",
            "smoothed_tpot_ms",
            "default_tpot_ms",
            "value_source",
        ]

        suffix = path.suffix.lower()
        if suffix == ".csv":
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: row.get(k) for k in fieldnames})
        elif suffix == ".xlsx":
            try:
                from openpyxl import Workbook
            except Exception:
                csv_fallback = str(path.with_suffix(".csv"))
                self.export_lengthwise_curve(csv_fallback, rows=rows)
                print(f"[TPOT][WARN] openpyxl unavailable, exported CSV fallback => {csv_fallback}")
                return

            wb = Workbook()
            ws = wb.active
            ws.title = "length_curve"
            ws.append(fieldnames)
            for row in rows:
                ws.append([row.get(k) for k in fieldnames])
            wb.save(path)
        else:
            path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"[TPOT] Exported length-wise curve => {path}")

    def export_json(self, output_path: str):
        payload = {
            "summary": self.build_summary(),
            "records": [asdict(rec) for records in self._records.values() for rec in records],
        }
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[TPOT] Exported benchmark result => {path}")
