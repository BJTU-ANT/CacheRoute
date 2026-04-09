"""Queue-side TTFT compute-time predictor.

Usage:
    from proxy.metrics.queue_predictor import queue_predictor
    t = queue_predictor(length=1024, bs=4)

Coefficient source priority:
1) explicit `coeffs` argument,
2) explicit `coeff_path`,
3) default `proxy/metrics/ttft_coefficients.json`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Optional


DEFAULT_COEFF_PATH = Path(__file__).with_name("ttft_coefficients.json")

def _short_length_compensation_seconds(length: int, bs: int) -> float:
    """
    短请求补偿项（经验值）：
    - 解决四项式在小长度区间低估/裁零的问题。
    - 当前先按 bs=1 的实验现象补偿；bs>1 时默认不额外补偿。
    """
    if bs != 1:
        return 0.0

    # 经验点：
    # - very short（~10 token）：实际约 60ms
    # - short（~89 token）：需补约 20ms
    # - mid-short（~345 token）：需补约 24ms
    if length <= 80:
        return 0.060
    if length <= 160:
        return 0.020
    if length <= 384:
        return 0.024
    return 0.0


def load_ttft_coefficients(coeff_path: str | Path = DEFAULT_COEFF_PATH) -> Dict[str, float]:
    """Load a/b/c/d coefficients from JSON file."""
    path = Path(coeff_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    try:
        coeffs = {
            "a": float(payload["a"]),
            "b": float(payload["b"]),
            "c": float(payload["c"]),
            "d": float(payload["d"]),
        }
    except KeyError as exc:
        raise ValueError(f"missing coefficient key: {exc}") from exc
    return coeffs


def queue_predictor(
    length: int,
    bs: Optional[int] = None,
    *,
    coeffs: Optional[Dict[str, float]] = None,
    coeff_path: str | Path = DEFAULT_COEFF_PATH,
) -> float:
    """Predict TTFT compute time by batch-size and prompt length.

    Args:
        length: prompt length.
        bs: batch size. if omitted/None, defaults to 1.
        coeffs: optional in-memory dict {a,b,c,d}.
        coeff_path: coefficient json path when coeffs is not provided.

    Returns:
        Predicted TTFT time (same unit as coefficient set, usually seconds).
    """
    if length <= 0:
        raise ValueError("length must be positive")

    batch_size = 1 if bs is None else int(bs)
    if batch_size <= 0:
        raise ValueError("bs must be positive")

    c = coeffs or load_ttft_coefficients(coeff_path)
    pred = (
        float(c["a"]) * (batch_size * int(length))
        + float(c["b"]) * int(length)
        + float(c["c"]) * batch_size
        + float(c["d"])
    )
    pred += _short_length_compensation_seconds(length=int(length), bs=batch_size)
    return max(0.0, pred)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Predict TTFT by length and batch-size.")
    parser.add_argument("--length", type=int, required=True, help="prompt length")
    parser.add_argument("--bs", type=int, default=1, help="batch size, default=1")
    parser.add_argument(
        "--coeff-path",
        type=str,
        default=str(DEFAULT_COEFF_PATH),
        help="coefficient json path",
    )
    parser.add_argument(
        "--ms",
        action="store_true",
        help="also print milliseconds for convenience",
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    pred_seconds = queue_predictor(
        length=args.length,
        bs=args.bs,
        coeff_path=args.coeff_path,
    )
    print(f"predicted_ttft_seconds={pred_seconds:.6f}")
    if args.ms:
        print(f"predicted_ttft_ms={pred_seconds * 1000:.3f}")


if __name__ == "__main__":
    main()
