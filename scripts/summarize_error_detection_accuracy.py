"""Summarizes data/validation/error_detection_accuracy.jsonl (written by
scripts/run_monitor_validation_sweep.py) into macro-F1 and a per-category/
per-tier breakdown.

Reports macro-F1, NOT raw accuracy — this video's real windows are ~88%
ground-truth-positive under CARES' heavily-overlapping windowing (confirmed
this session: 231/262 real windows), so a naive always-predict-error
baseline scores ~88% raw accuracy while being useless. Compared explicitly
against CARES' own reported 54.3 mF1 on this exact dataset as the honest
bar — a comparable or lower number here is expected on a single video with
project-authored (not CARES-published) tis/cis/alpha/threshold values, and
should be reported as such, not apologized for.

Usage: uv run scripts/summarize_error_detection_accuracy.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

VALIDATION_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "validation" / "error_detection_accuracy.jsonl"


def load_records() -> list[dict]:
    if not VALIDATION_LOG_PATH.exists():
        return []
    with open(VALIDATION_LOG_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def compute_prf1(records: list[dict]) -> dict:
    tp = sum(1 for r in records if r["predicted_error"] and r["ground_truth_error"])
    fp = sum(1 for r in records if r["predicted_error"] and not r["ground_truth_error"])
    fn = sum(1 for r in records if not r["predicted_error"] and r["ground_truth_error"])
    tn = sum(1 for r in records if not r["predicted_error"] and not r["ground_truth_error"])

    precision_pos = tp / (tp + fp) if (tp + fp) else 0.0
    recall_pos = tp / (tp + fn) if (tp + fn) else 0.0
    f1_pos = 2 * precision_pos * recall_pos / (precision_pos + recall_pos) if (precision_pos + recall_pos) else 0.0

    precision_neg = tn / (tn + fn) if (tn + fn) else 0.0
    recall_neg = tn / (tn + fp) if (tn + fp) else 0.0
    f1_neg = 2 * precision_neg * recall_neg / (precision_neg + recall_neg) if (precision_neg + recall_neg) else 0.0

    macro_f1 = (f1_pos + f1_neg) / 2
    raw_accuracy = (tp + tn) / len(records) if records else 0.0

    return {
        "n": len(records),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision_pos": precision_pos, "recall_pos": recall_pos, "f1_pos": f1_pos,
        "precision_neg": precision_neg, "recall_neg": recall_neg, "f1_neg": f1_neg,
        "macro_f1": macro_f1,
        "raw_accuracy": raw_accuracy,
        "positive_rate": (tp + fn) / len(records) if records else 0.0,
    }


def main() -> int:
    records = load_records()
    if not records:
        print(f"No records found at {VALIDATION_LOG_PATH} — run scripts/run_monitor_validation_sweep.py first.")
        return 1

    overall = compute_prf1(records)
    print(f"=== Overall ({overall['n']} windows) ===")
    print(f"  positive_rate (ground truth): {overall['positive_rate']:.1%}  <- if this is high, raw_accuracy is a misleading metric, use macro_f1")
    print(f"  raw_accuracy:  {overall['raw_accuracy']:.3f}")
    print(f"  macro_f1:      {overall['macro_f1']:.3f}   (CARES reports 54.3 mF1 [0.543] on this exact dataset as the honest comparison bar)")
    print(f"  f1 (error+):   {overall['f1_pos']:.3f}  (precision={overall['precision_pos']:.3f}, recall={overall['recall_pos']:.3f})")
    print(f"  f1 (normal-):  {overall['f1_neg']:.3f}  (precision={overall['precision_neg']:.3f}, recall={overall['recall_neg']:.3f})")
    print(f"  confusion: tp={overall['tp']} fp={overall['fp']} fn={overall['fn']} tn={overall['tn']}")

    by_category: dict[str, list[dict]] = defaultdict(list)
    by_tier: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if r.get("escalated_category"):
            by_category[r["escalated_category"]].append(r)
        if r.get("tier_used"):
            by_tier[r["tier_used"]].append(r)

    if by_category:
        print("\n=== By escalated category (only windows that escalated to pass-2) ===")
        for category, recs in sorted(by_category.items()):
            stats = compute_prf1(recs)
            print(f"  {category}: n={stats['n']} macro_f1={stats['macro_f1']:.3f} raw_accuracy={stats['raw_accuracy']:.3f}")

    if by_tier:
        print("\n=== By expertise tier (does risk-routing correlate with correctness?) ===")
        for tier, recs in sorted(by_tier.items()):
            stats = compute_prf1(recs)
            print(f"  {tier}: n={stats['n']} macro_f1={stats['macro_f1']:.3f} raw_accuracy={stats['raw_accuracy']:.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
