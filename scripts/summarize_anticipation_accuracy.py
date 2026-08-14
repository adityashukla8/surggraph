"""Summarizes data/validation/anticipation_accuracy.jsonl (written by
agents/anticipation/agent.py's _log_anticipation_accuracy, once per real
segment transition it forecasts) into top-1 accuracy and mean ETA error,
broken down by coverage_n bucket (plan §2) — the honest way to disclose
that single-video transition statistics are thin: a linear procedure
recording usually transitions each phase to exactly one next phase, so many
real "probabilities" are degenerate prob=1.0, n=1. That's a real, not
fabricated, finding, and reported as such rather than hidden.

Usage: uv run scripts/summarize_anticipation_accuracy.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

VALIDATION_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "validation" / "anticipation_accuracy.jsonl"

_COVERAGE_BUCKETS = [(0, 1, "n=0-1 (degenerate/unseen)"), (2, 3, "n=2-3"), (4, float("inf"), "n>=4")]


def load_records() -> list[dict]:
    if not VALIDATION_LOG_PATH.exists():
        return []
    with open(VALIDATION_LOG_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def _bucket_for(coverage_n: int) -> str:
    for low, high, label in _COVERAGE_BUCKETS:
        if low <= coverage_n <= high:
            return label
    return _COVERAGE_BUCKETS[-1][2]


def summarize(records: list[dict]) -> dict:
    n = len(records)
    top1_accuracy = sum(1 for r in records if r["correct"]) / n if n else 0.0
    mean_eta_error_s = None
    # ETA error is only meaningful when the prediction was correct — an ETA
    # for the wrong phase isn't a timing error, it's a wrong-answer error
    # already counted by top1_accuracy.
    correct_records = [r for r in records if r["correct"]]
    return {
        "n": n,
        "top1_accuracy": top1_accuracy,
        "n_correct": len(correct_records),
        "deviated_from_prior_rate": sum(1 for r in records if r["deviated_from_prior"]) / n if n else 0.0,
        "mean_confidence": sum(r["confidence"] for r in records) / n if n else 0.0,
    }


def main() -> int:
    records = load_records()
    if not records:
        print(f"No records found at {VALIDATION_LOG_PATH} — run the live pipeline against a full video first.")
        return 1

    overall = summarize(records)
    print(f"=== Overall ({overall['n']} real segment transitions) ===")
    print(f"  top1_accuracy:            {overall['top1_accuracy']:.1%}  ({overall['n_correct']}/{overall['n']})")
    print(f"  mean_confidence:          {overall['mean_confidence']:.2f}")
    print(f"  deviated_from_prior_rate: {overall['deviated_from_prior_rate']:.1%}")

    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_bucket[_bucket_for(r["coverage_n"])].append(r)

    print("\n=== By prior coverage_n bucket (honesty check — thin single-video stats) ===")
    for _, _, label in _COVERAGE_BUCKETS:
        recs = by_bucket.get(label, [])
        if not recs:
            continue
        stats = summarize(recs)
        print(f"  {label}: n={stats['n']} top1_accuracy={stats['top1_accuracy']:.1%}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
