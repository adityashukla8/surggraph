"""Tests for the fail-closed verification gate.

This is the component that exists to say no, so its behaviour is asserted
rather than trusted. Two things matter most: that it is genuinely read-only,
and that it blocks by default when it cannot verify something.

`evaluate_divergence_alert` is pure and takes an already-built graph index, so
every case below is a real graph shape rather than a mock.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.verification_gate import gate
from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch, StateSnapshot
from tools.context_slice import GraphIndex


def _node(node_id: str, node_type: str, attrs: dict, label: str = "x") -> GraphNodePatch:
    return GraphNodePatch(
        node_id=node_id, node_type=node_type, label=label, attrs=attrs,
        source_agent="test", source_tool="test", timestamp=datetime.now(timezone.utc),
    )


def _edge(src: str, tgt: str, kind: str) -> GraphEdgePatch:
    return GraphEdgePatch(
        edge_id=node_ids.edge(src, tgt, kind), source_node_id=src, target_node_id=tgt,
        edge_kind=kind, source_agent="test", source_tool="test",
    )


ERROR_ID = "error:w0010:tissue_handling"
COMPLICATION_ID = "complication:error:w0010:tissue_handling:bladder_neck_injury"
PROPOSAL_ID = "corrective:error:w0010:tissue_handling:reduce_traction"
ALERT_ID = "divergence:corrective:0"


def _build(**overrides) -> GraphIndex:
    """A complete, passing chain, with named overrides for breaking one link."""
    complication_attrs = {
        "evidence_backed": True, "confidence": 0.8, "root_error_id": ERROR_ID,
        "mechanism": "m", **overrides.get("complication", {}),
    }
    proposal_attrs = {
        "escalated": False, "complication_id": COMPLICATION_ID, "provenance": {"tier": 2},
        "steps": [{"order": 1, "action": "a", "verification_check": "v"}], "urgency": "prompt",
        **overrides.get("proposal", {}),
    }
    alert_attrs = {"proposal_id": PROPOSAL_ID, "confidence": 0.9, "advisory": False, **overrides.get("alert", {})}

    nodes = [
        _node(ERROR_ID, "error", {"severity_band": "medium", "error_category": "tissue_handling"}),
        _node(COMPLICATION_ID, "complication", complication_attrs),
        _node(PROPOSAL_ID, "corrective_trajectory", proposal_attrs),
        _node(ALERT_ID, "divergence_alert", alert_attrs),
    ]
    edges = [
        _edge(ERROR_ID, COMPLICATION_ID, "causal_reasoning"),
        _edge(COMPLICATION_ID, PROPOSAL_ID, "proposal"),
        _edge(PROPOSAL_ID, ALERT_ID, "trajectory_comparison"),
    ]
    if overrides.get("drop_causal_edge"):
        edges = [e for e in edges if e.edge_kind != "causal_reasoning"]
    if overrides.get("drop_proposal_edge"):
        edges = [e for e in edges if e.edge_kind != "proposal"]
    if overrides.get("drop_nodes"):
        nodes = [n for n in nodes if n.node_id not in overrides["drop_nodes"]]

    return GraphIndex(StateSnapshot(case_id="c", seq=1, nodes=nodes, edges=edges))


# --- Read-only, structurally -------------------------------------------------


def test_gate_imports_no_write_alert_or_fhir_tooling():
    """Read-only is enforced by what the module can reach, not by intention.

    Checked against the module's real resolved namespace rather than its source
    text — an earlier version of this check matched the phrase "no alerting
    tool" in the docstring and failed a module that was perfectly correct.
    """
    forbidden = {"send_alert", "write_document_reference", "requests", "httpx"}
    reachable = set(vars(gate).keys())
    assert not (forbidden & reachable), f"gate can reach {forbidden & reachable}"


def test_gate_does_not_perform_the_write_it_approves():
    """On a pass it returns a verdict; the caller acts. The gate must never be
    the thing that acts."""
    result = gate.evaluate_divergence_alert(_build(), ALERT_ID)
    assert result.passed is True
    assert isinstance(result, gate.GateResult)  # a verdict, not a delivery


# --- The happy path ----------------------------------------------------------


def test_complete_chain_passes():
    result = gate.evaluate_divergence_alert(_build(), ALERT_ID)
    assert result.passed
    assert result.block_reasons == []
    assert len(result.checks) >= 8


# --- Fail closed -------------------------------------------------------------


def test_ungrounded_complication_is_blocked():
    """The check this gate exists for: a complication its own reasoning step
    could not tie to retrieved evidence must not become a clinical alert."""
    result = gate.evaluate_divergence_alert(_build(complication={"evidence_backed": False}), ALERT_ID)
    assert not result.passed
    assert any("not literature-grounded" in r for r in result.block_reasons)


def test_escalation_is_blocked():
    result = gate.evaluate_divergence_alert(_build(proposal={"escalated": True}), ALERT_ID)
    assert not result.passed
    assert any("escalation" in r for r in result.block_reasons)


def test_missing_provenance_is_blocked():
    result = gate.evaluate_divergence_alert(_build(proposal={"provenance": {}}), ALERT_ID)
    assert not result.passed
    assert any("provenance" in r for r in result.block_reasons)


def test_low_complication_confidence_is_blocked():
    result = gate.evaluate_divergence_alert(_build(complication={"confidence": 0.2}), ALERT_ID)
    assert not result.passed
    assert any("confidence" in r for r in result.block_reasons)


def test_advisory_divergence_is_blocked():
    """An acknowledged proposal still logs divergences; they must not reach an
    external channel (docs §11)."""
    result = gate.evaluate_divergence_alert(_build(alert={"advisory": True}), ALERT_ID)
    assert not result.passed
    assert any("advisory" in r for r in result.block_reasons)


@pytest.mark.parametrize("missing", [COMPLICATION_ID, PROPOSAL_ID, ERROR_ID])
def test_a_missing_link_in_the_chain_is_blocked(missing):
    result = gate.evaluate_divergence_alert(_build(drop_nodes={missing}), ALERT_ID)
    assert not result.passed


def test_missing_causal_edge_is_blocked():
    """Attributes can be right while the graph is not. A viewer follows edges,
    so an alert whose chain is only assertable via attrs is not traceable."""
    result = gate.evaluate_divergence_alert(_build(drop_causal_edge=True), ALERT_ID)
    assert not result.passed
    assert any("causal_reasoning edge" in r for r in result.block_reasons)


def test_missing_proposal_edge_is_blocked():
    result = gate.evaluate_divergence_alert(_build(drop_proposal_edge=True), ALERT_ID)
    assert not result.passed


def test_unknown_alert_is_blocked_not_passed():
    """The default is no. An alert that is not on the graph cannot be verified,
    which is a block."""
    result = gate.evaluate_divergence_alert(_build(), "divergence:does-not-exist:0")
    assert not result.passed


def test_every_block_carries_a_reason():
    """A block with no reason is unactionable, and would render as a bare
    refusal on the graph."""
    for override in ({"complication": {"evidence_backed": False}}, {"proposal": {"escalated": True}}, {"alert": {"advisory": True}}):
        result = gate.evaluate_divergence_alert(_build(**override), ALERT_ID)
        assert not result.passed
        assert result.block_reasons and all(r.strip() for r in result.block_reasons)
