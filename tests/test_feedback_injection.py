"""plan_v2 §16.9 Step 4 — structural verification of the four real agent
injection points. Offline, no mocked GEAP/Firestore calls except where
noted (the Divergence Detection latency-regression test monkeypatches
feedback_block itself, to prove it's never even called on the deterministic
path — that's the property under test, not something to hide behind a real
call).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import agents.complication_reasoning.agent as complication_agent
import agents.corrective_replanning.agent as corrective_agent
import agents.divergence_detection.agent as divergence_agent
from state.schema import GraphNodePatch, StateSnapshot


# --- Byte-identity when empty (plan_v2 §16.5's structural requirement) -----


def test_format_for_judgment_empty_feedback_is_byte_identical_to_no_feedback_arg():
    proposal = _fake_node("corrective:e1:plan", "corrective_trajectory", "Test plan", {"urgency": "routine", "steps": []})
    observations = []
    with_default = divergence_agent._format_for_judgment(proposal, observations)
    with_explicit_empty = divergence_agent._format_for_judgment(proposal, observations, feedback="")
    assert with_default == with_explicit_empty


def test_format_for_judgment_includes_feedback_when_given():
    proposal = _fake_node("corrective:e1:plan", "corrective_trajectory", "Test plan", {"urgency": "routine", "steps": []})
    block = "REVIEWER FEEDBACK — advisory input from past case reviews, NOT ground truth.\nStanding guidance:\n  - test directive"
    rendered = divergence_agent._format_for_judgment(proposal, [], feedback=block)
    assert block in rendered
    # And the empty-feedback version must be a strict prefix-minus-block —
    # feedback is appended, never interleaved into the existing sections.
    without = divergence_agent._format_for_judgment(proposal, [], feedback="")
    assert rendered.startswith(without)


def test_complication_reasoning_format_slice_empty_feedback_byte_identical():
    slice_context = {"triggering_error": {"label": "test error", "attrs": {}}}
    with_default = complication_agent._format_slice(slice_context)
    with_explicit_empty = complication_agent._format_slice(slice_context, feedback="")
    assert with_default == with_explicit_empty


def test_complication_reasoning_format_slice_includes_feedback_when_given():
    slice_context = {"triggering_error": {"label": "test error", "attrs": {}}}
    block = "REVIEWER FEEDBACK — advisory input from past case reviews, NOT ground truth."
    rendered = complication_agent._format_slice(slice_context, feedback=block)
    assert block in rendered


def test_corrective_replanning_format_slice_empty_feedback_byte_identical():
    slice_context = {"triggering_complication": {"label": "test complication", "attrs": {}}}
    with_default = corrective_agent._format_slice(slice_context, "needle_handling")
    with_explicit_empty = corrective_agent._format_slice(slice_context, "needle_handling", feedback="")
    assert with_default == with_explicit_empty


def test_corrective_replanning_format_slice_includes_feedback_when_given():
    slice_context = {"triggering_complication": {"label": "test complication", "attrs": {}}}
    block = "REVIEWER FEEDBACK — advisory input from past case reviews, NOT ground truth."
    rendered = corrective_agent._format_slice(slice_context, "needle_handling", feedback=block)
    assert block in rendered


# --- The two complication_reasoning call sites must target DIFFERENT agents -


@pytest.mark.asyncio
async def test_complication_reasoning_real_function_calls_feedback_block_twice_with_different_agents(monkeypatch):
    """Real integration-lite test of reason_about_error itself, not just the
    two call expressions in isolation — the easiest real mistake here is
    copy-pasting one feedback_block call into both sites with the same
    target_agent literal, which would make literature_retrieval's directive
    (e.g. a recency preference) silently apply to complication_reasoning's
    own feedback instead, and vice versa. Every other dependency (Firestore
    snapshot, both LLM calls, literature retrieval, the graph write) is
    stubbed so this test is fast and offline while still exercising the
    real function body and real call order."""
    from agents.complication_reasoning.subagent import ComplicationAssessment, LiteratureQuery

    case_id = "case-complication-injection-test"
    error_node_id = "error:1:inj_test_category"
    error_node = _fake_node(error_node_id, "error", "test error", {"severity_band": "high", "error_category": "inj_test_category"})
    snapshot = StateSnapshot(case_id=case_id, seq=1, nodes=[error_node], edges=[])

    calls: list[tuple[str, str]] = []

    async def fake_feedback_block(target_agent, context_query, *a, **k):
        calls.append((target_agent, context_query))
        return ""

    async def fake_get_state_snapshot(cid):
        return snapshot

    async def fake_run_llm_agent_once(agent, content, output_model, app_name=None):
        if agent is complication_agent.QUERY_AGENT:
            return LiteratureQuery(queries=["a", "b"], rationale="test")
        return ComplicationAssessment(candidates=[], reasoning="nothing warranted for this test")

    async def fake_retrieve(cid, queries, parent_node_id=None):
        return [], [], False

    async def fake_apply_state_patches(cid, patches):
        return None

    monkeypatch.setattr(complication_agent, "feedback_block", fake_feedback_block)
    monkeypatch.setattr(complication_agent, "get_state_snapshot", fake_get_state_snapshot)
    monkeypatch.setattr(complication_agent, "run_llm_agent_once", fake_run_llm_agent_once)
    monkeypatch.setattr(complication_agent, "retrieve", fake_retrieve)
    monkeypatch.setattr(complication_agent, "apply_state_patches", fake_apply_state_patches)
    complication_agent._seen.clear()

    result = await complication_agent.reason_about_error(case_id, error_node_id)

    assert result == []  # no candidates warranted, per the stub above
    assert [c[0] for c in calls] == ["literature_retrieval", "complication_reasoning"], (
        f"expected exactly 2 feedback_block calls in this order, got: {calls}"
    )


# --- Divergence Detection: deterministic path must never call feedback_block


@pytest.mark.asyncio
async def test_divergence_deterministic_path_never_calls_feedback_block(monkeypatch):
    """Latency-regression guard (plan_v2 §16.5's explicit constraint): the
    deterministic recurrence check must stay free of any extra latency, so
    feedback_block must not be called at all when a recurrence is found —
    only on the semantic (LLM) path below it."""
    case_id = "case-feedback-injection-test"
    proposal_id = "corrective:err-inj-test:plan-inj-test"
    root_error_id = "error:1:inj_test_category"
    recurrence_error_id = "error:2:inj_test_category"

    now = datetime.now(timezone.utc)
    proposal_time = now - timedelta(seconds=60)
    recurrence_time = now - timedelta(seconds=10)  # AFTER proposal_time -> real recurrence

    root_error = _fake_node(root_error_id, "error", "root error", {"error_category": "inj_test_category"}, timestamp=now - timedelta(seconds=120))
    proposal = _fake_node(
        proposal_id, "corrective_trajectory", "Test plan",
        {"urgency": "routine", "steps": [], "root_error_id": root_error_id},
        timestamp=proposal_time,
    )
    recurrence = _fake_node(recurrence_error_id, "error", "recurrence", {"error_category": "inj_test_category"}, timestamp=recurrence_time)

    snapshot = StateSnapshot(case_id=case_id, seq=1, nodes=[root_error, proposal, recurrence], edges=[])

    async def fake_get_state_snapshot(cid):
        return snapshot

    async def fake_apply_state_patches(cid, patches):
        return None

    async def raise_if_called(*a, **k):
        raise AssertionError("feedback_block must NOT be called on the deterministic recurrence path")

    monkeypatch.setattr(divergence_agent, "get_state_snapshot", fake_get_state_snapshot)
    monkeypatch.setattr(divergence_agent, "apply_state_patches", fake_apply_state_patches)
    monkeypatch.setattr(divergence_agent, "feedback_block", raise_if_called)
    # Fresh module-level dedupe state so a prior test run in the same
    # process can't mask a real call to _write_alert.
    divergence_agent._alerted.clear()
    divergence_agent._alerted_evidence.clear()

    result = await divergence_agent.check_proposal(case_id, proposal_id)
    assert result is not None, "expected a real alert to be written on the deterministic recurrence path"


def _fake_node(node_id: str, node_type: str, label: str, attrs: dict, timestamp=None) -> GraphNodePatch:
    return GraphNodePatch(
        node_id=node_id,
        node_type=node_type,
        label=label,
        attrs=attrs,
        source_agent="test",
        source_tool="test",
        timestamp=timestamp or datetime.now(timezone.utc),
    )
