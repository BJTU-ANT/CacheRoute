"""TTFT four-term regressor for proxy-side estimation.

Model form:
    ttft = a * (batch_size * prompt_length) + b * prompt_length + c * batch_size + d

This module is intentionally lightweight and data-driven:
- accepts point-wise samples,
- supports loading matrix-style JSON benchmark tables,
- fits parameters with numpy least-squares,
- keeps prediction and coefficient export simple for strategy usage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

DEFAULT_COEFF_PATH = Path(__file__).with_name("ttft_coefficients.json")


@dataclass
class FourTermCoefficients:
    a: float
    b: float
    c: float
    d: float


class TTFTFourTermRegressor:
    """Fit and predict TTFT with 4-term linear features: [B*L, L, B, 1]."""

    def __init__(self) -> None:
        self._samples: List[Tuple[int, int, float]] = []
        self._coeffs: Optional[FourTermCoefficients] = None

    def add_sample(self, batch_size: int, prompt_length: int, ttft: float) -> None:
        if batch_size <= 0 or prompt_length <= 0:
            raise ValueError("batch_size and prompt_length must be positive")
        if ttft <= 0:
            raise ValueError("ttft must be positive")
        self._samples.append((batch_size, prompt_length, float(ttft)))

    def add_samples(self, samples: Iterable[Tuple[int, int, float]]) -> None:
        for batch_size, prompt_length, ttft in samples:
            self.add_sample(batch_size, prompt_length, ttft)

    def load_from_json(self, data_path: str | Path) -> int:
        """Load benchmark samples from matrix-style JSON.

        Expected JSON schema:
        {
          "ttft_unit": "ms" | "s",
          "lengths": [64, 320, ...],
          "rows": [
            {"batch_size": 1, "ttft": [86.22, 421.01, ...]},
            {"batch_size": 2, "ttft": [149.11, 858.0, ...]}
          ]
        }

        Notes:
        - null values in ttft list are skipped.
        - if ttft_unit == "ms", values are converted to seconds for fitting.

        Returns:
            Number of valid points loaded.
        """
        p = Path(data_path)
        payload = json.loads(p.read_text(encoding="utf-8"))

        unit = str(payload.get("ttft_unit", "ms")).lower()
        if unit not in {"ms", "s"}:
            raise ValueError(f"unsupported ttft_unit: {unit}")

        lengths = payload.get("lengths")
        rows = payload.get("rows")
        if not isinstance(lengths, list) or not isinstance(rows, list):
            raise ValueError("invalid json format: lengths/rows must be lists")

        loaded = 0
        for row in rows:
            batch_size = int(row["batch_size"])
            ttft_values = row.get("ttft", [])
            if len(ttft_values) != len(lengths):
                raise ValueError(
                    f"row batch_size={batch_size} has ttft length {len(ttft_values)} "
                    f"but lengths has {len(lengths)}"
                )

            for prompt_length, value in zip(lengths, ttft_values):
                if value is None:
                    continue
                ttft_raw = float(value)
                ttft_seconds = ttft_raw / 1000.0 if unit == "ms" else ttft_raw
                self.add_sample(batch_size, int(prompt_length), ttft_seconds)
                loaded += 1
        return loaded

    def fit(self) -> FourTermCoefficients:
        if len(self._samples) < 4:
            raise ValueError("at least 4 valid samples are required")

        x = np.array(
            [[bs * pl, pl, bs, 1.0] for bs, pl, _ in self._samples],
            dtype=np.float64,
        )
        y = np.array([ttft for _, _, ttft in self._samples], dtype=np.float64)

        # Least-squares fit: x @ coeff = y
        coeff, _, _, _ = np.linalg.lstsq(x, y, rcond=None)
        self._coeffs = FourTermCoefficients(
            a=float(coeff[0]), b=float(coeff[1]), c=float(coeff[2]), d=float(coeff[3])
        )
        return self._coeffs

    def save_coefficients_json(
        self,
        coeff_path: str | Path = DEFAULT_COEFF_PATH,
        *,
        unit: str = "seconds",
    ) -> Path:
        """Persist current coefficients to JSON for runtime queue prediction."""
        if self._coeffs is None:
            raise RuntimeError("regressor is not fitted yet")

        path = Path(coeff_path)
        payload = {
            "a": self._coeffs.a,
            "b": self._coeffs.b,
            "c": self._coeffs.c,
            "d": self._coeffs.d,
            "unit": unit,
            "source": "TTFTFourTermRegressor",
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def predict(self, batch_size: int, prompt_length: int) -> float:
        if self._coeffs is None:
            raise RuntimeError("regressor is not fitted yet")
        bs = float(batch_size)
        pl = float(prompt_length)
        pred = (
            self._coeffs.a * (bs * pl)
            + self._coeffs.b * pl
            + self._coeffs.c * bs
            + self._coeffs.d
        )
        return max(0.0, float(pred))

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


if __name__ == "__main__":
    data_file = Path(__file__).with_name("ttft_benchmark_table.json")
    regressor = TTFTFourTermRegressor()
    loaded = regressor.load_from_json(data_file)
    coeffs = regressor.fit()
    coeff_file = regressor.save_coefficients_json()

    print(f"[TTFT4] loaded points: {loaded}")
    print(
        "[TTFT4] coeffs (seconds): "
        f"a={coeffs.a:.6e}, b={coeffs.b:.6e}, c={coeffs.c:.6e}, d={coeffs.d:.6e}"
    )
    print(f"[TTFT4] coefficients saved to: {coeff_file}")
