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
DEFAULT_TPOT_COEFF_PATH = Path(__file__).with_name("tpot_coefficients.json")
DEFAULT_REDIS_PULL_COEFF_PATH = Path(__file__).with_name("redis_pull_coefficients.json")
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


def load_redis_pull_coefficients(
    coeff_path: str | Path = DEFAULT_REDIS_PULL_COEFF_PATH,
) -> Dict[str, float]:
    path = Path(coeff_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    try:
        coeffs = {
            "a": float(payload["a"]),
            "b": float(payload["b"]),
        }
    except KeyError as exc:
        raise ValueError(f"missing coefficient key: {exc}") from exc
    return coeffs


def load_tpot_coefficients(coeff_path: str | Path = DEFAULT_TPOT_COEFF_PATH) -> Dict[str, float]:
    """Load TPOT a/b/c coefficients from JSON file."""
    path = Path(coeff_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    try:
        coeffs = {
            "a": float(payload["a"]),
            "b": float(payload["b"]),
            "c": float(payload["c"]),
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


def decode_tpot_predictor(
    length: int,
    bs: Optional[int] = None,
    *,
    coeffs: Optional[Dict[str, float]] = None,
    coeff_path: str | Path = DEFAULT_TPOT_COEFF_PATH,
) -> float:
    """Predict per-token TPOT(decode) time in seconds by batch-size and length."""
    if length <= 0:
        raise ValueError("length must be positive")

    batch_size = 1 if bs is None else int(bs)
    if batch_size <= 0:
        raise ValueError("bs must be positive")

    c = coeffs or load_tpot_coefficients(coeff_path)
    pred = (
        float(c["a"]) * batch_size
        + float(c["b"]) * int(length)
        + float(c["c"])
    )
    return max(0.0, float(pred))


def predict_redis_pull_ms(
    *,
    kvcache_size_gb: float,
    coeffs: Optional[Dict[str, float]] = None,
    coeff_path: str | Path = DEFAULT_REDIS_PULL_COEFF_PATH,
) -> float:
    """
    Predict LMCache redis pull time in milliseconds.
    Linear form:
      redis_pull_ms = a * kvcache_size_gb + b
    """
    x = max(0.0, float(kvcache_size_gb))
    c = coeffs or load_redis_pull_coefficients(coeff_path)
    pred = float(c["a"]) * x + float(c["b"])
    return max(0.0, pred)


def align_hit_length_tokens(knowledge_length_tokens: int, *, align_size: int = 256) -> int:
    """Align knowledge hit length by fixed token granularity (default 256)."""
    if align_size <= 0:
        raise ValueError("align_size must be positive")
    klen = max(0, int(knowledge_length_tokens))
    return (klen // align_size) * align_size


def estimate_kvcache_size_gb(
    *,
    knowledge_length_tokens: int,
    kv_gb_per_token: float = 0.0000381,
    align_size: int = 256,
) -> float:
    hit_tokens = align_hit_length_tokens(knowledge_length_tokens, align_size=align_size)
    return max(0.0, float(hit_tokens) * float(kv_gb_per_token))


def predict_prefill_and_redis_breakdown(
    *,
    total_length_tokens: int,
    knowledge_length_tokens: int,
    bs: int = 1,
    kv_gb_per_token: float = 0.0000381,
    align_size: int = 256,
    ttft_coeff_path: str | Path = DEFAULT_COEFF_PATH,
    redis_coeff_path: str | Path = DEFAULT_REDIS_PULL_COEFF_PATH,
) -> Dict[str, float]:
    """
    Predict pure-compute + redis-pull timing in one place with unified inputs.
    - total_length_tokens -> compute input
    - knowledge_length_tokens -> redis pull input (with 256 alignment)
    """
    total = max(0, int(total_length_tokens))
    hit_tokens = min(align_hit_length_tokens(knowledge_length_tokens, align_size=align_size), total)
    remaining_compute_tokens = max(1, total - hit_tokens)
    compute_seconds = queue_predictor(length=remaining_compute_tokens, bs=bs, coeff_path=ttft_coeff_path)
    kvcache_size_gb = max(0.0, float(hit_tokens) * float(kv_gb_per_token))
    redis_pull_ms = predict_redis_pull_ms(kvcache_size_gb=kvcache_size_gb, coeff_path=redis_coeff_path)
    compute_ms = max(0.0, compute_seconds * 1000.0)
    return {
        "total_length_tokens": float(total),
        "knowledge_length_tokens": float(max(0, int(knowledge_length_tokens))),
        "actual_hit_length_tokens": float(hit_tokens),
        "remaining_compute_tokens": float(remaining_compute_tokens),
        "kvcache_size_gb": float(kvcache_size_gb),
        "predicted_pure_compute_ms": float(compute_ms),
        "predicted_redis_pull_ms": float(redis_pull_ms),
        "predicted_kvcache_total_ms": float(compute_ms + redis_pull_ms),
    }


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
    parser.add_argument(
        "--kvcache-size-gb",
        type=float,
        default=None,
        help="optional: predict redis pull ms using redis_pull_coefficients.json",
    )
    parser.add_argument(
        "--redis-coeff-path",
        type=str,
        default=str(DEFAULT_REDIS_PULL_COEFF_PATH),
        help="redis pull coefficient json path",
    )
    parser.add_argument(
        "--knowledge-length",
        type=int,
        default=None,
        help="knowledge length tokens for KVCache-based structured estimation; will be aligned by --align-size",
    )
    parser.add_argument("--align-size", type=int, default=256, help="alignment size for knowledge hit tokens")
    parser.add_argument(
        "--kv-gb-per-token",
        type=float,
        default=0.0000381,
        help="KVCache size(GB) per token for kvcache-size estimation",
    )
    parser.add_argument(
        "--decode-length",
        type=int,
        default=None,
        help="optional: decode predictor input length tokens",
    )
    parser.add_argument(
        "--decode-bs",
        type=int,
        default=1,
        help="decode predictor batch size, default=1",
    )
    parser.add_argument(
        "--tpot-coeff-path",
        type=str,
        default=str(DEFAULT_TPOT_COEFF_PATH),
        help="TPOT coefficient json path",
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
    pred_ms = pred_seconds * 1000.0
    print("[text-based]")
    print(f"  total_length_tokens={int(args.length)}")
    print(f"  predicted_compute_seconds={pred_seconds:.6f}")
    print(f"  predicted_compute_ms={pred_ms:.3f}")

    if args.kvcache_size_gb is not None:
        pull_ms = predict_redis_pull_ms(
            kvcache_size_gb=float(args.kvcache_size_gb),
            coeff_path=args.redis_coeff_path,
        )
        print("[kvcache-size-only]")
        print(f"  kvcache_size_gb={float(args.kvcache_size_gb):.8f}")
        print(f"  predicted_redis_pull_ms={pull_ms:.3f}")
    if args.knowledge_length is not None:
        breakdown = predict_prefill_and_redis_breakdown(
            total_length_tokens=int(args.length),
            knowledge_length_tokens=int(args.knowledge_length),
            bs=int(args.bs),
            kv_gb_per_token=float(args.kv_gb_per_token),
            align_size=int(args.align_size),
            ttft_coeff_path=args.coeff_path,
            redis_coeff_path=args.redis_coeff_path,
        )
        print("[kvcache-based]")
        print(f"  total_length_tokens={int(breakdown['total_length_tokens'])}")
        print(f"  knowledge_length_tokens={int(breakdown['knowledge_length_tokens'])}")
        print(f"  actual_hit_length_tokens={int(breakdown['actual_hit_length_tokens'])}")
        print(f"  kvcache_size_gb={breakdown['kvcache_size_gb']:.8f}")
        print(f"  remaining_compute_tokens={int(breakdown['remaining_compute_tokens'])}")
        print(f"  predicted_remaining_compute_ms={breakdown['predicted_pure_compute_ms']:.3f}")
        print(f"  predicted_redis_pull_ms={breakdown['predicted_redis_pull_ms']:.3f}")
        print(f"  predicted_kvcache_total_ms={breakdown['predicted_kvcache_total_ms']:.3f}")
    if args.decode_length is not None:
        decode_seconds = decode_tpot_predictor(
            length=int(args.decode_length),
            bs=int(args.decode_bs),
            coeff_path=args.tpot_coeff_path,
        )
        print("[decode-per-token]")
        print(f"  decode_length_tokens={int(args.decode_length)}")
        print(f"  decode_bs={int(args.decode_bs)}")
        print(f"  predicted_decode_tpot_seconds={decode_seconds:.9f}")
        print(f"  predicted_decode_tpot_ms={decode_seconds * 1000.0:.6f}")


if __name__ == "__main__":
    main()
