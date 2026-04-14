"""Redis pull-time regressor for KVCache injection experiments.

Model form (milliseconds):
    redis_pull_ms = a * kvcache_size_gb + b

Input dataset can be CSV or JSON.
Common fields:
  - kvcache_size_gb
  - either redis_pull_ms OR redis_pull_ms_1..N OR redis_pull_ms(list)

When multiple redis_pull_ms_* columns exist, row target uses median value.
Negative samples are clipped to 0 for fitting stability.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_COEFF_PATH = Path(__file__).with_name("redis_pull_coefficients.json")


@dataclass
class RedisPullCoefficients:
    a: float
    b: float


def _to_float(v: object) -> Optional[float]:
    try:
        return float(v)  # type: ignore[arg-type]
    except Exception:
        return None


class RedisPullLinearRegressor:
    def __init__(self) -> None:
        self._samples: List[Tuple[float, float]] = []
        self._coeffs: Optional[RedisPullCoefficients] = None

    def add_sample(self, kvcache_size_gb: float, redis_pull_ms: float) -> None:
        if kvcache_size_gb < 0:
            raise ValueError("kvcache_size_gb must be >= 0")
        self._samples.append((float(kvcache_size_gb), max(0.0, float(redis_pull_ms))))

    def load_from_csv(self, data_path: str | Path) -> int:
        p = Path(data_path)
        with p.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise ValueError("csv has no header")

            ms_cols = [c for c in reader.fieldnames if c.startswith("redis_pull_ms_")]
            single_ms_col = "redis_pull_ms" if "redis_pull_ms" in reader.fieldnames else None

            loaded = 0
            for row in reader:
                x = _to_float(row.get("kvcache_size_gb"))
                if x is None:
                    continue

                y: Optional[float] = None
                if ms_cols:
                    vals = [_to_float(row.get(c)) for c in ms_cols]
                    vals = [v for v in vals if v is not None]
                    if vals:
                        y = float(statistics.median(vals))
                elif single_ms_col is not None:
                    y = _to_float(row.get(single_ms_col))

                if y is None:
                    continue

                self.add_sample(x, y)
                loaded += 1
            return loaded

    def load_from_json(self, data_path: str | Path) -> int:
        p = Path(data_path)
        payload = json.loads(p.read_text(encoding="utf-8"))
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise ValueError("invalid json format: 'rows' must be a list")

        loaded = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            x = _to_float(row.get("kvcache_size_gb"))
            if x is None:
                continue

            y: Optional[float] = None
            if isinstance(row.get("redis_pull_ms"), list):
                vals = [_to_float(v) for v in row.get("redis_pull_ms", [])]
                vals = [v for v in vals if v is not None]
                if vals:
                    y = float(statistics.median(vals))
            elif row.get("redis_pull_ms") is not None:
                y = _to_float(row.get("redis_pull_ms"))
            else:
                ms_keys = sorted([k for k in row.keys() if str(k).startswith("redis_pull_ms_")])
                vals = [_to_float(row.get(k)) for k in ms_keys]
                vals = [v for v in vals if v is not None]
                if vals:
                    y = float(statistics.median(vals))

            if y is None:
                continue
            self.add_sample(x, y)
            loaded += 1
        return loaded

    def load_from_file(self, data_path: str | Path) -> int:
        p = Path(data_path)
        if p.suffix.lower() == ".json":
            return self.load_from_json(p)
        return self.load_from_csv(p)

    def fit(self) -> RedisPullCoefficients:
        if len(self._samples) < 2:
            raise ValueError("at least 2 valid samples are required")

        xs = [x for x, _ in self._samples]
        ys = [y for _, y in self._samples]
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)

        num = sum((x - x_mean) * (y - y_mean) for x, y in self._samples)
        den = sum((x - x_mean) ** 2 for x in xs)

        if den <= 0:
            a = 0.0
            b = y_mean
        else:
            a = num / den
            b = y_mean - a * x_mean

        self._coeffs = RedisPullCoefficients(a=float(a), b=float(b))
        return self._coeffs

    def predict_ms(self, kvcache_size_gb: float) -> float:
        if self._coeffs is None:
            raise RuntimeError("regressor is not fitted yet")
        pred = self._coeffs.a * float(kvcache_size_gb) + self._coeffs.b
        return max(0.0, float(pred))

    def save_coefficients_json(
        self,
        coeff_path: str | Path = DEFAULT_COEFF_PATH,
        *,
        feature: str = "kvcache_size_gb",
    ) -> Path:
        if self._coeffs is None:
            raise RuntimeError("regressor is not fitted yet")
        path = Path(coeff_path)
        payload: Dict[str, object] = {
            "a": self._coeffs.a,
            "b": self._coeffs.b,
            "unit": "ms",
            "feature": feature,
            "source": "RedisPullLinearRegressor",
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fit redis pull-time linear model from CSV/JSON.")
    p.add_argument("--data-file", required=True, help="input data path (.csv or .json)")
    p.add_argument(
        "--coeff-path",
        default=str(DEFAULT_COEFF_PATH),
        help="output coefficient json path",
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    reg = RedisPullLinearRegressor()
    loaded = reg.load_from_file(args.data_file)
    coeffs = reg.fit()
    out = reg.save_coefficients_json(args.coeff_path)
    print(f"[RedisPull] loaded samples: {loaded}")
    print(f"[RedisPull] coeffs(ms): a={coeffs.a:.6f}, b={coeffs.b:.6f}")
    print(f"[RedisPull] coefficients saved to: {out}")


if __name__ == "__main__":
    main()
