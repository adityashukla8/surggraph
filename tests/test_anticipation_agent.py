"""Tests for the Anticipation Agent (plan §13.3, second revision — free-form
semantic reasoning, no numeral ever shown to Gemini or the UI). Real-data
assertions against the actual computed data/priors/phase_transition_matrix.json
and video_01 annotations, matching the style already established in
test_monitor_agent.py/test_scene_graph_builder.py — not mocked-everything.
Gemini-call-dependent forecasting itself is exercised live (Orchestrator
end-to-end), not here.
"""

from __future__ import annotations

import asyncio
import uuid

from agents.anticipation.agent import AGENT, _labels_match, _slugify
from agents.anticipation.subagent import AnticipationOutput
from state.schema import GraphEdgePatch, GraphNodePatch
from tools.action_labels import load_action_segments
from tools.phase_transition_priors import get_phase_transition_priors, summarize_transition_confidence
from tools.state_tools import apply_state_patch, get_state_snapshot

VIDEO_ID = "video_01"


# --- tools/phase_transition_priors.py: real, computed, never authored -------


def test_priors_are_real_computed_data_not_authored():
    ann = load_action_segments(VIDEO_ID)
    real_phase = str(ann[0].action_id)
    result = get_phase_transition_priors(real_phase)

    assert result["current_phase"] == real_phase
    assert result["coverage_n"] > 0
    probs = [c["probability"] for c in result["candidates"]]
    assert probs == sorted(probs, reverse=True)
    assert abs(sum(probs) - 1.0) < 1e-6


def test_priors_unknown_phase_returns_empty_not_fabricated():
    result = get_phase_transition_priors("nonexistent_phase_id")
    assert result["candidates"] == []
    assert result["coverage_n"] == 0


def test_summarize_transition_confidence_never_leaks_category_labels():
    # Real phase "0" has real candidates "1" and "6" (data/priors/phase_transition_matrix.json)
    # — the whole point of the anonymized hint is that neither ever appears
    # in the text handed to Gemini, only the real statistical SHAPE.
    hint = summarize_transition_confidence("0")
    assert "1" not in hint.split() and "'1'" not in hint
    assert "6" not in hint.split() and "'6'" not in hint
    assert "historical" in hint.lower() or "consistent" in hint.lower() or "ambiguous" in hint.lower()


def test_summarize_transition_confidence_unknown_phase_is_honest():
    hint = summarize_transition_confidence("nonexistent_phase_id")
    assert "no historical" in hint.lower()


# --- slug-based convergence matching -----------------------------------------


def test_slugify_normalizes_text():
    assert _slugify("Suturing / Closure!!") == "suturing_closure"
    assert _slugify("  Bladder-Neck   Dissection ") == "bladder_neck_dissection"


def test_labels_match_handles_exact_and_substring_and_rejects_unrelated():
    assert _labels_match("Suturing", "Suturing")
    assert _labels_match("Suturing", "Suturing and closure of the incision")
    assert _labels_match("Bladder neck dissection", "Bladder neck dissection begins")
    # Real, disclosed limitation (plan §13.3): word-order differences are
    # NOT matched — this is exact/substring text matching, not semantic
    # equivalence, and deliberately doesn't pretend otherwise.
    assert not _labels_match("Bladder neck dissection", "Dissection of the bladder neck")
    assert not _labels_match("Suturing", "Docking the robot")
    assert not _labels_match("", "Suturing")


# --- agent construction: no tools, no numeral anywhere -----------------------


def test_anticipation_agent_has_no_tools():
    # Corrected design (plan §13.3): Gemini has no legitimate way to know
    # which internal numeric key to pass to a tool, so the real transition-
    # prior data is now summarized into the prompt by the WRAPPER
    # (tools/phase_transition_priors.py::summarize_transition_confidence),
    # never fetched by Gemini itself mid-reasoning.
    assert AGENT.tools == []
    assert AGENT.output_schema is AnticipationOutput


def test_anticipation_output_has_no_numeric_phase_fields():
    # Structural guard against regressing to the original flaw (ground-
    # truth-fed numeral) or the rejected exemplar-ID-matching fix (opaque
    # numeral as output) — every phase-referring field must be real free
    # text the model produced itself.
    fields = AnticipationOutput.model_fields
    assert fields["current_phase_name"].annotation is str
    assert fields["next_phase_name"].annotation is str
    assert "current_phase_estimate" not in fields  # the rejected exemplar-matching field name
    assert "next_phase" not in fields  # the original flawed bare-numeral field name
    assert "prior_top_candidate" not in fields  # would leak an opaque id into the output


# --- tools/state_tools.py::get_state_snapshot — the read path used for convergence


def test_get_state_snapshot_local_fallback_round_trip(monkeypatch):
    monkeypatch.delenv("STATE_SERVICE_URL", raising=False)
    case_id = f"test-case-{uuid.uuid4().hex[:8]}"

    async def run():
        await apply_state_patch(
            case_id,
            node=GraphNodePatch(node_id="phase:1", node_type="phase", label="Phase 1", source_agent="test", source_tool="test"),
            reason="first write",
        )
        await apply_state_patch(
            case_id,
            node=GraphNodePatch(node_id="phase:1", node_type="phase", label="Phase 1 (updated)", source_agent="test", source_tool="test"),
            reason="second write",
        )
        await apply_state_patch(
            case_id,
            edge=GraphEdgePatch(
                edge_id="edge:a", source_node_id="phase:1", target_node_id="phase:2",
                edge_kind="prediction", confirmation_signal="pending", source_agent="test", source_tool="test",
            ),
            reason="predicted",
        )
        return await get_state_snapshot(case_id)

    snapshot = asyncio.run(run())
    assert len(snapshot.nodes) == 1
    assert snapshot.nodes[0].label == "Phase 1 (updated)"
    assert len(snapshot.edges) == 1
    assert snapshot.edges[0].confirmation_signal == "pending"


def test_get_state_snapshot_filters_removed_edges(monkeypatch):
    monkeypatch.delenv("STATE_SERVICE_URL", raising=False)
    case_id = f"test-case-{uuid.uuid4().hex[:8]}"

    async def run():
        await apply_state_patch(
            case_id,
            edge=GraphEdgePatch(edge_id="edge:x", source_node_id="a", target_node_id="b", edge_kind="prediction", source_agent="t", source_tool="t"),
            reason="created",
        )
        from tools.state_tools import RUNTIME_DIR
        from state.schema import StateDiffEvent

        path = RUNTIME_DIR / f"{case_id}_graph_patches.jsonl"
        removed_event = StateDiffEvent(
            case_id=case_id, seq=0, op="remove_edge",
            edge=GraphEdgePatch(edge_id="edge:x", source_node_id="a", target_node_id="b", edge_kind="prediction", source_agent="t", source_tool="t"),
            reason="removed", source_agent="t", source_tool="t",
        )
        with open(path, "a") as f:
            f.write(removed_event.model_dump_json() + "\n")
        return await get_state_snapshot(case_id)

    snapshot = asyncio.run(run())
    assert snapshot.edges == []
