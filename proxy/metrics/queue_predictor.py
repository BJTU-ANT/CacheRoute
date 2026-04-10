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
_SHORT_CALIB_ANCHORS_MS = [
    # (length_token, ttft_ms)
    (0, 60.0),
    (10, 60.0),
    (35, 47.0),
    (45, 53.26),
    (55, 65.97),
    (65, 70.71),
    (75, 76.63),
    (89, 88.38),
    (95, 90.35),
    (105, 93.01),
    (115, 95.67),
    (303, 396.0),
    (345, 472.0),
]


def _interp_by_anchors_ms(length: int, anchors: list[tuple[int, float]]) -> float:
    """Linear interpolation on monotonic-x anchor points, returning milliseconds."""
    if length <= anchors[0][0]:
        return anchors[0][1]
    if length > anchors[-1][0]:
        return 0.0

    for i in range(1, len(anchors)):
        x0, y0 = anchors[i - 1]
        x1, y1 = anchors[i]
        if length <= x1:
            ratio = (length - x0) / float(x1 - x0)
            return y0 + ratio * (y1 - y0)
    return 0.0

def _short_length_calibrated_seconds(length: int, bs: int) -> float:
    """
    短请求最小时延曲线（经验值）：
    - 解决四项式在小长度区间低估/裁零的问题。
    - 返回“该长度下建议的标定总时延（秒）”。
    - 当前先按 bs=1 的实验曲线生效；bs>1 暂不启用。
    """
    if bs != 1:
        return 0.0

    ms = _interp_by_anchors_ms(length=length, anchors=_SHORT_CALIB_ANCHORS_MS)
    return max(0.0, ms / 1000.0)


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
    calibrated_pred = _short_length_calibrated_seconds(length=int(length), bs=batch_size)

    # 小长度（<=115）直接采用标定曲线，允许对四项式做“下修”或“上修”。
    if batch_size == 1 and int(length) <= 115 and calibrated_pred > 0:
        return calibrated_pred

    # 中短长度保守策略：只做下限保护，避免低估。
    return max(pred, calibrated_pred)


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
