"""Benchmark Agent — the case grades its own detections at close.

docs/agentic_workflow.md §5 agent 10, docs/plan_v2 §6 step 11.

DETERMINISTIC. No model call, for the same reason the verification gate has
none: this is arithmetic over graph state and ground truth, and a model asked a
fixed question would give varying answers to it. Scoring that moves between
runs is not scoring.

THE AXIS IS BINARY, AND THAT IS A DATA FACT, NOT A SIMPLIFICATION.
plan_v2 §6 step 11 asks for per-category precision/recall/F1 via a CARES-6 to
OCHRA-24 mapping. That cannot be computed from what exists: the real
annotation file (`data/annotations/{video}/error_annotation.pkl`) contains
`error_GT` as a binary array — inspected, dtype int64, unique values {0, 1} —
with no category labels anywhere. The `sedmamba_fine_types` mapping in the
knowledge library covers 9 of 24 codes and is moot regardless, because those
codes are not in the data.

So this scores what the ground truth actually says: for each window, did we
flag an error, and was there one. Per-category counts are reported alongside as
DESCRIPTIVE ONLY — how many of each kind we fired — and are explicitly not
scored, because there is nothing to score them against.

THE SCORING IS THE SAME CODE PATH AS THE OFFLINE VALIDATION SWEEP. It imports
`compute_prf1` from scripts/summarize_error_detection_accuracy.py rather than
recomputing the same formula, so a case's own scorecard is directly comparable
to the tracked numbers in docs/validation_results.md instead of being a second
implementation that might quietly disagree.

GROUND TRUTH ENTERS HERE AND NOWHERE ELSE. This is the one legitimate place
(plan_v2 §6 step 11), post-hoc and never in a live decision path — a test
asserts the detection modules do not import the sidecar at all.
"""

from __future__ import annotations

import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.summarize_error_detection_accuracy import compute_prf1  # noqa: E402

from state import node_ids  # noqa: E402
from state.schema import GraphEdgePatch, GraphNodePatch  # noqa: E402
from tools.context_slice import GraphIndex  # noqa: E402
from tools.sedmamba_labels import (  # noqa: E402  VALIDATION-ONLY, post-hoc
    generate_windows,
    load_error_annotations,
    window_ground_truth,
)
from tools.state_tools import apply_state_patches, get_state_snapshot  # noqa: E402
from tools.video_utils import DEFAULT_WINDOW_S  # noqa: E402

logger = logging.getLogger(__name__)

SOURCE_AGENT = "benchmark"
_SOURCE_TOOL = "grade_case"

# CARES' published macro-F1 on this exact dataset. The honest comparison bar,
# quoted from the paper — not a target chosen here.
CARES_PUBLISHED_MACRO_F1 = 0.543


class GroundTruthUnavailable(Exception):
    """No annotations for this video. Benchmarking is skipped and said so —
    never substituted with an estimate."""


def _windows_we_swept(video_id: str, start_s: float, end_s: float, window_s: float):
    """The same windows the sweep actually ran, regenerated from the real
    sample grid so each one lines up with real ground-truth samples."""
    annotations = load_error_annotations(video_id)
    windows = generate_windows(
        annotations,
        window_s=window_s,
        # Non-overlapping, matching the live sweep: run_error_detection_sweep
        # is called with stride_s=window_s. A different stride here would score
        # windows the system never actually looked at.
        stride_s=window_s,
        start_s=start_s,
        end_s=end_s,
    )
    return annotations, windows


def grade(index: GraphIndex, video_id: str, start_s: float, end_s: float, window_s: float = DEFAULT_WINDOW_S) -> dict:
    """Aligns the case's own error nodes to ground truth. Pure — no I/O beyond
    reading the annotation file, so it is testable from a snapshot."""
    annotations, windows = _windows_we_swept(video_id, start_s, end_s, window_s)
    if not windows:
        raise GroundTruthUnavailable(f"no windows generated for {video_id} over {start_s}-{end_s}s")

    # Which windows we fired on. Error node ids are error:{window_id}:{category}
    # (state/node_ids.py), so the window is recoverable without re-deriving it.
    fired_windows: set[str] = set()
    categories = Counter()
    for node in index.of_type("error"):
        window_id = node.attrs.get("window_id")
        if window_id:
            fired_windows.add(str(window_id))
        category = node.attrs.get("error_category")
        if category:
            categories[category] += 1

    records = [
        {
            "window_id": w.window_id,
            "predicted_error": w.window_id in fired_windows,
            "ground_truth_error": window_ground_truth(annotations, w),
        }
        for w in windows
    ]

    metrics = compute_prf1(records)
    return {
        "axis": "binary",
        "axis_note": (
            "Scored on the binary error/no-error axis because the real ground truth is binary — "
            "error_GT contains only 0 and 1, with no category labels. Per-category scoring is not "
            "possible from this data."
        ),
        "video_id": video_id,
        "swept_range_s": [start_s, end_s],
        "window_s": window_s,
        **metrics,
        "cares_published_macro_f1": CARES_PUBLISHED_MACRO_F1,
        "vs_cares": round(metrics["macro_f1"] - CARES_PUBLISHED_MACRO_F1, 3),
        # Descriptive only. There is nothing to score these against.
        "category_counts_unscored": dict(categories),
    }


async def benchmark_case(case_id: str, video_id: str, start_s: float, end_s: float, window_s: float = DEFAULT_WINDOW_S) -> str | None:
    """Grades the case and writes a benchmark node. Returns its id, or None
    when there is no ground truth — in which case nothing is written, because
    an unscored case must not carry a scorecard."""
    index = GraphIndex(await get_state_snapshot(case_id))

    try:
        scorecard = grade(index, video_id, start_s, end_s, window_s)
    except (FileNotFoundError, GroundTruthUnavailable) as exc:
        logger.warning("benchmark[%s]: no ground truth for %s — not scoring (%s)", case_id, video_id, exc)
        return None

    node_id = node_ids.benchmark(case_id)
    label = (
        f"Self-benchmark: macro-F1 {scorecard['macro_f1']:.3f} "
        f"({'+' if scorecard['vs_cares'] >= 0 else ''}{scorecard['vs_cares']:.3f} vs CARES {CARES_PUBLISHED_MACRO_F1})"
    )

    patches: list[tuple] = [
        (
            GraphNodePatch(
                node_id=node_id,
                node_type="benchmark",
                label=label,
                attrs=scorecard,
                source_agent=SOURCE_AGENT,
                source_tool=_SOURCE_TOOL,
            ),
            None,
            f"Graded {scorecard['n']} windows against ground truth",
        )
    ]

    # A grading edge from each error node the case produced, so the scorecard
    # traces to what it graded rather than being an assertion about the case.
    for node in index.of_type("error"):
        patches.append(
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(node.node_id, node_id, "grading"),
                    source_node_id=node.node_id,
                    target_node_id=node_id,
                    edge_kind="grading",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                    reason="Graded against ground truth at case close",
                ),
                "Graded against ground truth at case close",
            )
        )

    await apply_state_patches(case_id, patches)
    logger.info(
        "benchmark[%s]: %s  (tp=%d fp=%d fn=%d tn=%d over %d windows)",
        case_id,
        label,
        scorecard["tp"],
        scorecard["fp"],
        scorecard["fn"],
        scorecard["tn"],
        scorecard["n"],
    )
    return node_id
