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

    # 用“分段线性插值”做连续补偿，避免分段跳变导致的非单调现象。
    # 锚点单位：token / ms
    anchors = [
        (0, 60.0),     # very short: 约 60ms 基线
        (64, 58.0),    # 接近你观测到的 64 token 区间
        (96, 32.0),    # 从短请求向中短请求平滑衰减
        (160, 24.0),
        (384, 24.0),   # 345 token 左右仍保留约 24ms 补偿
        (512, 0.0),    # 长请求逐步回归四项式主导
    ]

    if length <= anchors[0][0]:
        return anchors[0][1] / 1000.0
    if length >= anchors[-1][0]:
        return anchors[-1][1] / 1000.0

    for i in range(1, len(anchors)):
        x0, y0 = anchors[i - 1]
        x1, y1 = anchors[i]
        if length <= x1:
            ratio = (length - x0) / float(x1 - x0)
            y = y0 + ratio * (y1 - y0)
            return max(0.0, y / 1000.0)

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
    base_pred = (
        float(c["a"]) * (batch_size * int(length))
        + float(c["b"]) * int(length)
        + float(c["c"]) * batch_size
        + float(c["d"])
    )
    # 先裁零再补偿，避免短请求在 base_pred<0 时被“抵消”成过小值。
    pred = max(0.0, base_pred)
    pred += _short_length_compensation_seconds(length=int(length), bs=batch_size)
    return pred


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
