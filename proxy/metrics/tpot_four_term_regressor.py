"""TPOT four-term regressor for proxy-side decode estimation.

Model form:
    tpot = a * (batch_size * length) + b * batch_size + c * length + d

Notes:
- TPOT here means per-token decode time.
- input/output unit is seconds.
- matrix-style JSON loading keeps format close to TTFT regressor usage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

DEFAULT_COEFF_PATH = Path(__file__).with_name("tpot_coefficients.json")


@dataclass
class FourTermCoefficients:
    a: float
    b: float
    c: float
    d: float


class TPOTFourTermRegressor:
    """Fit and predict TPOT with 4-term linear features: [B*L, B, L, 1]."""

    def __init__(self) -> None:
        self._samples: List[Tuple[int, int, float]] = []
        self._coeffs: Optional[FourTermCoefficients] = None

    def add_sample(self, batch_size: int, length: int, tpot: float) -> None:
        if batch_size <= 0 or length <= 0:
            raise ValueError("batch_size and length must be positive")
        if tpot <= 0:
            raise ValueError("tpot must be positive")
        self._samples.append((batch_size, int(length), float(tpot)))

    def add_samples(self, samples: Iterable[Tuple[int, int, float]]) -> None:
        for batch_size, length, tpot in samples:
            self.add_sample(batch_size, length, tpot)

    def load_from_json(self, data_path: str | Path) -> int:
        """Load matrix-style benchmark samples.

        Expected schema:
        {
          "tpot_unit": "ms" | "s",
          "lengths": [64, 320, ...],
          "rows": [
            {"batch_size": 1, "tpot": [2.1, 2.3, ...]},
            {"batch_size": 2, "tpot": [2.8, 3.2, ...]}
          ]
        }
        """
        payload = json.loads(Path(data_path).read_text(encoding="utf-8"))
        unit = str(payload.get("tpot_unit", "ms")).lower()
        if unit not in {"ms", "s"}:
            raise ValueError(f"unsupported tpot_unit: {unit}")

        lengths = payload.get("lengths")
        rows = payload.get("rows")
        if not isinstance(lengths, list) or not isinstance(rows, list):
            raise ValueError("invalid json format: lengths/rows must be lists")

        loaded = 0
        for row in rows:
            batch_size = int(row["batch_size"])
            tpot_values = row.get("tpot", [])
            if len(tpot_values) != len(lengths):
                raise ValueError(
                    f"row batch_size={batch_size} has tpot length {len(tpot_values)} "
                    f"but lengths has {len(lengths)}"
                )
            for length, value in zip(lengths, tpot_values):
                if value is None:
                    continue
                raw = float(value)
                seconds = raw / 1000.0 if unit == "ms" else raw
                self.add_sample(batch_size=batch_size, length=int(length), tpot=seconds)
                loaded += 1
        return loaded

    def fit(
        self,
        *,
        lambda_interaction: float = 1e-3,
        lambda_bs: float = 0.0,
        lambda_length: float = 1e-2,
    ) -> FourTermCoefficients:
        if len(self._samples) < 4:
            raise ValueError("at least 4 valid samples are required")
        if lambda_interaction < 0 or lambda_bs < 0 or lambda_length < 0:
            raise ValueError("ridge penalties must be non-negative")

        x = np.array(
            [[bs * length, bs, length, 1.0] for bs, length, _ in self._samples],
            dtype=np.float64,
        )
        y = np.array([tpot for _, _, tpot in self._samples], dtype=np.float64)

        # Ridge with per-feature penalties:
        # - interaction/length terms are penalized by default to keep TPOT
        #   primarily batch-size sensitive (observed empirical pattern).
        # - intercept stays unpenalized.
        reg = np.diag([lambda_interaction, lambda_bs, lambda_length, 0.0])
        xtx = x.T @ x
        xty = x.T @ y
        coeff = np.linalg.solve(xtx + reg, xty)
        self._coeffs = FourTermCoefficients(
            a=float(coeff[0]), b=float(coeff[1]), c=float(coeff[2]), d=float(coeff[3])
        )
        return self._coeffs

    def predict(self, batch_size: int, length: int) -> float:
        if self._coeffs is None:
            raise RuntimeError("regressor is not fitted yet")
        bs = float(batch_size)
        l = float(length)
        pred = self._coeffs.a * (bs * l) + self._coeffs.b * bs + self._coeffs.c * l + self._coeffs.d
        return max(0.0, float(pred))

    def save_coefficients_json(
        self,
        coeff_path: str | Path = DEFAULT_COEFF_PATH,
        *,
        unit: str = "seconds",
    ) -> Path:
        if self._coeffs is None:
            raise RuntimeError("regressor is not fitted yet")
        path = Path(coeff_path)
        payload = {
            "a": self._coeffs.a,
            "b": self._coeffs.b,
            "c": self._coeffs.c,
            "d": self._coeffs.d,
            "unit": unit,
            "source": "TPOTFourTermRegressor",
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def get_coefficients(self) -> Dict[str, float]:
        if self._coeffs is None:
            raise RuntimeError("regressor is not fitted yet")
        return {
            "a": self._coeffs.a,
            "b": self._coeffs.b,
            "c": self._coeffs.c,
            "d": self._coeffs.d,
        }

    @property
    def sample_count(self) -> int:
        return len(self._samples)


def write_matrix_json_from_triplets(
    triplets: Iterable[Tuple[int, int, float]],
    output_path: str | Path,
    *,
    unit: str = "ms",
) -> Path:
    """Convert plain (bs, length, tpot) records to matrix-style JSON."""
    if unit not in {"ms", "s"}:
        raise ValueError("unit must be 'ms' or 's'")

    rows: Dict[int, Dict[int, float]] = {}
    lengths = set()
    for batch_size, length, value in triplets:
        bs = int(batch_size)
        l = int(length)
        rows.setdefault(bs, {})[l] = float(value)
        lengths.add(l)

    ordered_lengths = sorted(lengths)
    payload_rows = []
    for bs in sorted(rows):
        row_values = [rows[bs].get(l) for l in ordered_lengths]
        payload_rows.append({"batch_size": bs, "tpot": row_values})

    payload = {
        "tpot_unit": unit,
        "lengths": ordered_lengths,
        "rows": payload_rows,
    }
    out = Path(output_path)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    data_file = Path(__file__).with_name("tpot_benchmark_table.json")
    regressor = TPOTFourTermRegressor()
    loaded = regressor.load_from_json(data_file)
    coeffs = regressor.fit()
    coeff_file = regressor.save_coefficients_json()

    print(f"[TPOT4] loaded points: {loaded}")
    print(
        "[TPOT4] coeffs (seconds): "
        f"a={coeffs.a:.6e}, b={coeffs.b:.6e}, c={coeffs.c:.6e}, d={coeffs.d:.6e}"
    )
    print(f"[TPOT4] coefficients saved to: {coeff_file}")
