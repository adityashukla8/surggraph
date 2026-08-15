"""Summarizes data/validation/anticipation_accuracy.jsonl (written by
agents/anticipation/agent.py's _log_anticipation_accuracy, once per real
window it forecasts) into the two metrics that are honestly scoreable
without a semantic-name ground-truth legend — none exists for this
dataset, so "was the phase NAME right" can't be scored directly (see
agents/anticipation/agent.py's own docstring):

  - Change-point accuracy: did Gemini's own current-phase description
    change exactly when the real (ground-truth) phase actually changed
    between consecutive windows of the same real sweep, and stay stable
    when it didn't? A real signal for whether live perception is tracking
    real transitions, without needing to know if its chosen NAME for a
    phase is "the" correct one.
  - Self-consistency: does the same real phase id tend to get a similar
    description across different windows/runs that land on it? A real
    signal for whether the reasoning is stable, not a random restatement
    each time.

Usage: uv run scripts/summarize_anticipation_accuracy.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from agents.anticipation.agent import _labels_match

VALIDATION_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "validation" / "anticipation_accuracy.jsonl"


def load_records() -> list[dict]:
    if not VALIDATION_LOG_PATH.exists():
        return []
    with open(VALIDATION_LOG_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def _change_point_confusion(records: list[dict]) -> dict:
    """Groups by (video_id, case_id) — never compares consecutive windows
    across two different real case runs — sorts by real window_start_s,
    scores each consecutive pair as a real tp/fp/fn/tn against whether the
    ground-truth phase id actually changed."""
    by_run: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        by_run[(r["video_id"], r["case_id"])].append(r)

    tp = fp = fn = tn = 0
    for run_records in by_run.values():
        run_records.sort(key=lambda r: r["window_start_s"])
        for prev, curr in zip(run_records, run_records[1:]):
            real_changed = prev["real_numeric_phase_id"] != curr["real_numeric_phase_id"]
            gemini_changed = not _labels_match(prev["gemini_current_phase_name"], curr["gemini_current_phase_name"])
            if real_changed and gemini_changed:
                tp += 1
            elif real_changed and not gemini_changed:
                fn += 1
            elif not real_changed and gemini_changed:
                fp += 1
            else:
                tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"n": tp + fp + fn + tn, "tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": precision, "recall": recall, "f1": f1}


def _self_consistency(records: list[dict]) -> dict:
    """Groups by real_numeric_phase_id across ALL logged runs (the real id
    means the same real segment content regardless of which case observed
    it) and checks what fraction of same-id PAIRS get a matching
    (slugified) description."""
    by_phase: dict[str, list[str]] = defaultdict(list)
    for r in records:
        if r["real_numeric_phase_id"] is not None:
            by_phase[r["real_numeric_phase_id"]].append(r["gemini_current_phase_name"])

    matched = 0
    total_pairs = 0
    per_phase: dict[str, dict] = {}
    for phase_id, names in by_phase.items():
        if len(names) < 2:
            continue
        phase_matched = sum(1 for a, b in combinations(names, 2) if _labels_match(a, b))
        phase_total = len(names) * (len(names) - 1) // 2
        matched += phase_matched
        total_pairs += phase_total
        per_phase[phase_id] = {"n_observations": len(names), "consistency_rate": phase_matched / phase_total if phase_total else 0.0}

    return {
        "overall_consistency_rate": matched / total_pairs if total_pairs else 0.0,
        "total_pairs_compared": total_pairs,
        "per_phase": per_phase,
    }


def main() -> int:
    records = load_records()
    if not records:
        print(f"No records found at {VALIDATION_LOG_PATH} — run the live pipeline first.")
        return 1

    print(f"=== Overall ({len(records)} real forecasts logged) ===")
    mean_current_conf = sum(r["gemini_current_phase_confidence"] for r in records) / len(records)
    mean_next_conf = sum(r["gemini_next_phase_confidence"] for r in records) / len(records)
    print(f"  mean current-phase confidence: {mean_current_conf:.2f}")
    print(f"  mean next-phase confidence:    {mean_next_conf:.2f}")

    cp = _change_point_confusion(records)
    print(f"\n=== Change-point accuracy ({cp['n']} consecutive real window pairs, within-run only) ===")
    print("  Did Gemini's own description change exactly when the real ground-truth phase changed?")
    print(f"  precision={cp['precision']:.2f} recall={cp['recall']:.2f} f1={cp['f1']:.2f}")
    print(f"  confusion: tp={cp['tp']} fp={cp['fp']} fn={cp['fn']} tn={cp['tn']}")

    sc = _self_consistency(records)
    print(f"\n=== Self-consistency ({sc['total_pairs_compared']} same-real-phase pairs compared) ===")
    print(f"  overall_consistency_rate: {sc['overall_consistency_rate']:.1%}")
    for phase_id, stats in sorted(sc["per_phase"].items()):
        print(f"  phase {phase_id}: n_observations={stats['n_observations']} consistency_rate={stats['consistency_rate']:.1%}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
