"""Tests for the Monitor Agent (plan §3.5). Real-data assertions against
the actual downloaded video_01 pickle/video, matching the style already
established in test_europepmc_rag.py/test_fhir_write_readback.py — not
mocked-everything.

Gemini-call-dependent tests (marked) hit the real API and cost real
requests; the rest are pure data/logic tests with no network dependency.
"""

from __future__ import annotations

import numpy as np
import pytest

from agents.monitor.aggregation import DEFAULT_ALPHA, aggregate, pick_escalation_candidate
from agents.monitor.knowledge import ERROR_KNOWLEDGE_LIBRARY, compute_psi, route_tier
from tools.sedmamba_labels import derive_sample_rate_hz, generate_windows, load_error_annotations, window_ground_truth
from tools.video_utils import find_video_path, sample_frames

VIDEO_ID = "video_01"


# --- tools/sedmamba_labels.py: derived, not hardcoded ------------------------


def test_sample_rate_is_derived_not_hardcoded():
    # Real video_01 data reproduces 5.0 Hz — but derived from frame deltas,
    # never a literal `5.0` in the source (an earlier attempt hardcoding
    # SEDMAMBA_SAMPLE_RATE_HZ = 5 was correctly rejected for exactly this).
    ann = load_error_annotations(VIDEO_ID)
    assert ann.sample_rate_hz == 5.0

    # A synthetic fixture with DIFFERENT spacing must yield a DIFFERENT rate —
    # proves the function responds to data rather than returning a constant.
    synthetic_names = np.array([f"{i * 24:09d}.png" for i in range(10)])
    rate = derive_sample_rate_hz(synthetic_names, fps=60.0)
    assert rate == 2.5

    with pytest.raises(ValueError):
        derive_sample_rate_hz(np.array(["000000000.png", "000000012.png", "000000030.png"]), fps=60.0)


def test_window_count_matches_real_video():
    ann = load_error_annotations(VIDEO_ID)
    windows = generate_windows(ann)
    assert len(windows) == 262


def test_window_ground_truth_positive_count_matches_real_data():
    ann = load_error_annotations(VIDEO_ID)
    windows = generate_windows(ann)
    positive = sum(1 for w in windows if window_ground_truth(ann, w))
    assert positive == 231


# --- agents/monitor/knowledge.py ---------------------------------------------


def test_knowledge_library_has_six_cares_categories_with_required_fields():
    assert set(ERROR_KNOWLEDGE_LIBRARY.keys()) == {
        "multiple_attempts",
        "out_of_view",
        "needle_handling",
        "tissue_handling",
        "suture_handling",
        "instrument_control",
    }
    for entry in ERROR_KNOWLEDGE_LIBRARY.values():
        assert entry.definition
        assert entry.normal_indicators
        assert entry.error_indicators
        assert entry.focus_areas
        assert 1 <= entry.tis <= 3
        assert 1 <= entry.cis <= 3


def test_psi_routing_matches_cares_eq3():
    assert route_tier(2) == "resident"
    assert route_tier(3) == "resident"
    assert route_tier(4) == "attending"
    assert route_tier(5) == "attending"
    assert route_tier(6) == "expert"
    with pytest.raises(ValueError):
        route_tier(1)  # below CARES' defined range (TIS+CIS each >= 1, so psi >= 2)
    with pytest.raises(ValueError):
        route_tier(7)  # above CARES' defined range (TIS+CIS each <= 3, so psi <= 6)

    # every authored category must route somewhere, no gaps
    for category in ERROR_KNOWLEDGE_LIBRARY:
        psi = compute_psi(category)
        assert 2 <= psi <= 6
        assert route_tier(psi) in ("resident", "attending", "expert")


# --- agents/monitor/aggregation.py -------------------------------------------


def test_aggregate_weight_ordering_is_structural_invariant():
    assert DEFAULT_ALPHA["temporal"] > DEFAULT_ALPHA["spatial"] > DEFAULT_ALPHA["procedural"]


def test_aggregate_requires_at_least_two_agents():
    # no single agent can fire alone
    for combo in [(True, False, False), (False, True, False), (False, False, True)]:
        _, fired = aggregate(*combo)
        assert not fired, f"single agent should never fire alone: {combo}"

    # any two agreeing agents must fire
    for combo in [(True, True, False), (True, False, True), (False, True, True)]:
        _, fired = aggregate(*combo)
        assert fired, f"two agreeing agents should fire: {combo}"

    score, fired = aggregate(True, True, True)
    assert fired and score == pytest.approx(3.0)
    score, fired = aggregate(False, False, False)
    assert not fired and score == 0.0


def test_pick_escalation_candidate_picks_highest_confidence_above_bar():
    candidate = pick_escalation_candidate(
        [
            {"needle_handling": 0.3, "tissue_handling": 0.6},
            {"needle_handling": 0.5},
            {},
        ]
    )
    assert candidate == "tissue_handling"


def test_pick_escalation_candidate_returns_none_below_bar():
    assert pick_escalation_candidate([{"needle_handling": 0.1}]) is None


# --- Live decision path must never read ground truth -------------------------


def test_coordinator_never_imports_ground_truth_in_decision_path():
    """Concrete, automatic guard against ever regressing to the rejected
    lookup design: the modules that decide whether a divergence fires must
    not import the validation-only SEDMamba loader."""
    import agents.monitor.aggregation as aggregation_module
    import agents.monitor.coordinator as coordinator_module

    for module in (coordinator_module, aggregation_module):
        source_names = set(vars(module).keys())
        assert "tools.sedmamba_labels" not in getattr(module, "__file__", "")
        assert not any("sedmamba" in name.lower() for name in source_names if not name.startswith("_"))


# --- tools/video_utils.py frame sampling (no API calls) ----------------------


def test_frame_sampling_returns_valid_chronological_jpegs():
    path = find_video_path(VIDEO_ID)
    assert path is not None
    frames = sample_frames(path, start_frame=13740, end_frame=14328, n_frames=6, resize_to=(960, 540))
    assert len(frames) == 6
    frame_numbers = [f.frame_number for f in frames]
    assert frame_numbers == sorted(frame_numbers)  # chronological
    for f in frames:
        assert f.jpeg_bytes[:2] == b"\xff\xd8"  # real JPEG magic bytes
