"""Offline validation sweep for the Monitor Agent (plan §3.5 §7): runs the
real live 2-pass detection pipeline over a range of windows and scores each
against SEDMamba's real ground truth — never the other way around. This is
the ONLY place tools/sedmamba_labels.py's ground truth is compared against
a Monitor decision; agents/monitor/coordinator.py's live decision path never
imports it (enforced by tests/test_monitor_agent.py).

Distinct from the live demo path (agents/monitor/agent.py::monitor_case,
which never touches ground truth and only emits graph patches for whatever
fires): this script emits NO graph patches, is safe to run repeatedly for
tuning, and its only output is the validation log + the demo-beat file.

Real throughput finding (this session): a single Gemini call over one
window's frame sample takes ~13-30s; Vertex AI's shared-pool concurrency
for gemini-3.5-flash on this project is contention-sensitive (transient
429s under even moderate concurrency, resolved via SDK-level retry in
tools/gemini_model.py) — a full 262-window sweep at default settings is a
genuinely long-running job (likely 1+ hour), not a quick script. This tool
logs per-window progress so a long run is observable, not a silent black
box, and supports resuming a bounded range rather than always running the
whole video.

Usage:
    uv run scripts/run_monitor_validation_sweep.py video_01
    uv run scripts/run_monitor_validation_sweep.py video_01 --start-s 200 --end-s 260
    uv run scripts/run_monitor_validation_sweep.py video_01 --stride-s 5  # coarser, faster
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from agents.monitor.coordinator import run_monitor_window
from tools.sedmamba_labels import generate_windows, load_error_annotations, log_window_accuracy, window_ground_truth

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
VALIDATION_LOG_PATH = DATA_ROOT / "validation" / "monitor_accuracy.jsonl"
DEMO_BEAT_PATH = DATA_ROOT / "validation" / "monitor_demo_beat.json"


async def run_sweep(
    video_id: str,
    start_s: float,
    end_s: float | None,
    stride_s: float,
    window_s: float,
    max_concurrent_windows: int,
) -> None:
    annotations = load_error_annotations(video_id)
    windows = generate_windows(annotations, window_s=window_s, stride_s=stride_s, start_s=start_s, end_s=end_s)
    print(f"Sweeping {len(windows)} window(s) for {video_id} (start_s={start_s}, end_s={end_s}, stride_s={stride_s})")

    semaphore = asyncio.Semaphore(max_concurrent_windows)
    completed = 0
    t_start = time.time()
    best_true_positive: dict | None = None

    async def process(window):
        nonlocal completed, best_true_positive
        async with semaphore:
            t0 = time.time()
            try:
                assessment = await run_monitor_window(video_id, window)
            except Exception as e:
                completed += 1
                print(f"[{completed}/{len(windows)}] {window.window_id} FAILED after {time.time() - t0:.1f}s: {type(e).__name__}: {str(e)[:150]}")
                return

            gt = window_ground_truth(annotations, window)
            correct = assessment.is_divergence == gt
            completed += 1
            elapsed = time.time() - t0
            print(
                f"[{completed}/{len(windows)}] {window.window_id} "
                f"predicted={assessment.is_divergence} ground_truth={gt} "
                f"{'OK' if correct else 'WRONG'} (score={assessment.composite_score:.2f}, {elapsed:.1f}s)"
            )

            log_window_accuracy(
                VALIDATION_LOG_PATH,
                {
                    "video_id": video_id,
                    "window_id": window.window_id,
                    "start_frame": window.start_frame,
                    "end_frame": window.end_frame,
                    "escalated_category": assessment.escalated_category,
                    "psi": assessment.psi,
                    "tier_used": assessment.tier_used,
                    "composite_score": assessment.composite_score,
                    "threshold_used": assessment.threshold_used,
                    "predicted_error": assessment.is_divergence,
                    "ground_truth_error": gt,
                    "correct": correct,
                },
            )

            # Track the best true-positive (predicted=True, ground_truth=True,
            # highest margin above threshold) for the demo-beat selection —
            # per plan §3.5's demo-reliability strategy: this only decides
            # WHERE to point the camera, never fakes the live detection itself.
            if assessment.is_divergence and gt:
                margin = assessment.composite_score - assessment.threshold_used
                if best_true_positive is None or margin > best_true_positive["margin"]:
                    best_true_positive = {
                        "window_id": window.window_id,
                        "start_s": window.start_s,
                        "end_s": window.end_s,
                        "composite_score": assessment.composite_score,
                        "threshold_used": assessment.threshold_used,
                        "margin": margin,
                        "escalated_category": assessment.escalated_category,
                    }

    await asyncio.gather(*(process(w) for w in windows))

    print(f"\nSweep finished in {time.time() - t_start:.1f}s ({completed}/{len(windows)} windows completed)")

    if best_true_positive is not None:
        DEMO_BEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DEMO_BEAT_PATH, "w") as f:
            json.dump({**best_true_positive, "video_id": video_id, "generated_at": datetime.now(timezone.utc).isoformat()}, f, indent=2)
        print(f"Best demo beat: {best_true_positive['window_id']} (margin={best_true_positive['margin']:.2f}) -> {DEMO_BEAT_PATH}")
    else:
        print("No true-positive window found in this range — no demo beat written.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video_id")
    parser.add_argument("--start-s", type=float, default=0.0)
    parser.add_argument("--end-s", type=float, default=None)
    parser.add_argument("--stride-s", type=float, default=1.0, help="CARES default is 1.0 (heavy overlap); widen for a faster, coarser sweep")
    parser.add_argument("--window-s", type=float, default=10.0)
    parser.add_argument("--max-concurrent-windows", type=int, default=2, help="kept low given this project's Dynamic Shared Quota contention (see module docstring)")
    args = parser.parse_args()

    asyncio.run(
        run_sweep(args.video_id, args.start_s, args.end_s, args.stride_s, args.window_s, args.max_concurrent_windows)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
