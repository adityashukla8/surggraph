"""Tests for Scene Graph Builder (plan §12). No live Gemini calls here —
matching test_monitor_agent.py's own convention, those are real but slow
(~163s median per window, confirmed this session) and were run manually
with full real input/output shown separately, not baked into the routine
suite. These are the pure data/logic/structural checks.
"""

from __future__ import annotations

import pytest

from agents.scene_graph_builder.agent import _generate_windows
from agents.scene_graph_builder.subagent import SceneEntity, SceneGraphWindowOutput, SceneRelation
from tools.segmentation_masks import load_colorized_mask_jpeg, load_mask_index, nearest_mask_path
from tools.video_utils import find_video_fps

VIDEO_ID = "video_01"


# --- Windowing: non-overlapping, frame-derived, not hardcoded ---------------


def test_windows_are_non_overlapping_and_cover_the_full_range():
    fps = find_video_fps(VIDEO_ID)
    windows = _generate_windows(98.0, 125.0, 10.0, fps)
    assert len(windows) == 3
    # non-overlapping: each window's end_s equals the next one's start_s
    for a, b in zip(windows, windows[1:]):
        assert a.end_s == b.start_s
    # covers exactly the requested range, no gap, no overrun
    assert windows[0].start_s == 98.0
    assert windows[-1].end_s == 125.0
    # the final window is a real partial chunk (25 - 27*10 doesn't divide evenly), not padded/rounded
    assert windows[-1].end_s - windows[-1].start_s == pytest.approx(7.0)


def test_window_frame_boundaries_derive_from_real_fps():
    fps = find_video_fps(VIDEO_ID)
    windows = _generate_windows(0.0, 10.0, 10.0, fps)
    assert len(windows) == 1
    assert windows[0].start_frame == 0
    assert windows[0].end_frame == round(10.0 * fps)


def test_single_window_when_range_is_exactly_one_window():
    fps = find_video_fps(VIDEO_ID)
    windows = _generate_windows(98.0, 108.0, 10.0, fps)
    assert len(windows) == 1
    assert windows[0].window_id == "scenegraph-w0000"


# --- tools/segmentation_masks.py --------------------------------------------


def test_nearest_mask_path_handles_frame_zero():
    """Regression guard: an earlier implementation used `min(...) and
    index[min(...)]`, which silently returned 0 instead of the real path
    whenever the nearest real mask frame number was itself 0 (falsy int),
    since `0 and x` short-circuits to 0. video_01's real first mask IS at
    frame 0, so this is a real, reachable case, not a contrived one."""
    path = nearest_mask_path(VIDEO_ID, 0)
    assert path is not None
    assert path.name == "000000000.png"


def test_nearest_mask_path_picks_the_real_closest_frame():
    index = load_mask_index(VIDEO_ID)
    frame_numbers = sorted(index.keys())
    # a point roughly mid-way between the 2nd and 3rd real masks
    probe = (frame_numbers[1] + frame_numbers[2]) // 2
    path = nearest_mask_path(VIDEO_ID, probe, mask_index=index)
    nearest_real_frame = min(frame_numbers, key=lambda f: abs(f - probe))
    assert path == index[nearest_real_frame]


def test_load_colorized_mask_jpeg_produces_valid_jpeg():
    path = nearest_mask_path(VIDEO_ID, 5880)
    assert path is not None
    jpeg_bytes = load_colorized_mask_jpeg(path)
    assert jpeg_bytes[:2] == b"\xff\xd8"  # real JPEG magic bytes


# --- Structured output schema -----------------------------------------------


def test_scene_entity_rejects_out_of_range_confidence():
    with pytest.raises(ValueError):
        SceneEntity(entity_id="x", entity_type="instrument", label="x", confidence=1.5)


def test_scene_graph_window_output_allows_relation_with_no_target():
    # a relation without a real target is valid output (the graph-writing
    # side simply skips creating an edge for it — see agent.py)
    output = SceneGraphWindowOutput(
        entities=[SceneEntity(entity_id="e1", entity_type="instrument", label="e1", confidence=0.9)],
        relations=[SceneRelation(subject_entity_id="e1", verb="idle", target_entity_id=None, confidence=0.5)],
        activity_description="nothing notable",
        reasoning="test",
    )
    assert output.relations[0].target_entity_id is None


# --- Ground truth as input context (plan §12) — the deliberate opposite of
# Error Detection's "never imports ground truth" guard, reflecting the different,
# explicit design decision for this agent -----------------------------------


def test_scene_graph_builder_legitimately_uses_real_ground_truth_as_input_context():
    """Confirms the plan §12 decision is actually implemented, not just
    described: Scene Graph Builder's module DOES import the real phase
    tooling — feeding a real (but unlabeled) structural signal to the model
    is the deliberate design here, unlike Error Detection's live decision path.

    Segmentation-mask context was deliberately dropped (not an oversight)
    as of docs/latency_optimization.md's restructuring — real, confirmed
    per-image tiling cost with no matching latency benefit; the phase-ID
    signal is negligible cost and stays. tools/segmentation_masks.py itself
    still exists and is still used elsewhere (scripts/prepare_demo_videos.py)."""
    import agents.scene_graph_builder.agent as sgb_module

    source_names = set(vars(sgb_module).keys())
    assert "phase_at_frame" in source_names
    assert not any("segmentation" in name.lower() or "mask" in name.lower() for name in source_names)


# --- ADK multi-parent regression guard --------------------------------------


def test_orchestrator_agent_can_be_constructed_more_than_once():
    """Regression guard: an ADK agent instance can only ever have one
    parent, permanently. An earlier version of both ErrorDetectionCoordinatorAgent
    and OrchestratorAgent declared `sub_agents=` using shared module-level
    singleton instances — fine for a single construction, but a second
    construction raised a real pydantic ValidationError ("already has a
    parent agent"). Constructing three times here is the real regression
    test — a live test suite constructing this more than once is exactly
    the scenario that broke."""
    from agents.orchestrator.agent import OrchestratorAgent

    for _ in range(3):
        agent = OrchestratorAgent()
        # Anticipation is deliberately absent — docs/agentic_workflow.md's
        # roster has no card for it, so it is not dispatched. Its code still
        # exists under agents/anticipation/; this asserts the roster, not that
        # the module was deleted.
        assert {s.name for s in agent.sub_agents} == {"error_detection_coordinator", "scene_graph_builder"}
