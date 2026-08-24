"""Verification Gate — fail-closed, read-only over the graph.

docs/agentic_workflow.md §3 agent 9, docs/plan_v2 §6 step 9.

Every external write passes through here first. The gate walks the reasoning
chain behind the proposed action and refuses anything with a hole in it.

FAIL-CLOSED MEANS THE DEFAULT IS NO. A check that cannot be evaluated — a node
that is missing, an attribute that is absent, an exception mid-walk — blocks.
Anything else would make the gate a formality: a safety check that passes when
it is confused is worse than no check, because it manufactures confidence.

READ-ONLY IS STRUCTURAL, NOT A PROMISE. This module imports no write tool, no
alerting tool, and no FHIR tool. It writes exactly one thing, its own outcome
node, through the shared state API. It cannot perform the write it approves —
on a pass it RETURNS pass, and the caller does the write. That separation is
what stops the gate from ever being the thing that acts.

PASSES ARE VISIBLE TOO. Recording only blocks would make the gate look like it
only ever objects, and would leave an approved external write with no record of
having been checked at all.

THIS IS DELIBERATELY NOT AN LLM. Every check below is a structural fact about
the graph — does this node exist, is this attribute true, is this number above
this threshold. A model asked "is this reasoning chain sound?" would answer
plausibly and inconsistently, and the one place in the system that exists to
say no is the worst possible place for a judgment that varies between runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch
from tools.context_slice import GraphIndex
from tools.state_tools import apply_state_patches, get_state_snapshot

logger = logging.getLogger(__name__)

SOURCE_AGENT = "verification_gate"
_SOURCE_TOOL = "verify_external_write"

# Floors below which a link in the chain is too weak to act on externally.
# Project-authored, exposed here rather than buried in an expression so they
# can be argued with and re-tuned.
MIN_COMPLICATION_CONFIDENCE = 0.5
MIN_DIVERGENCE_CONFIDENCE = 0.6


@dataclass
class GateResult:
    passed: bool
    checks: list[dict] = field(default_factory=list)
    block_reasons: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.passed:
            return f"Passed {len(self.checks)} checks"
        return "; ".join(self.block_reasons)


def _check(result: GateResult, name: str, ok: bool, detail: str) -> bool:
    result.checks.append({"check": name, "passed": bool(ok), "detail": detail})
    if not ok:
        result.block_reasons.append(f"{name}: {detail}")
    return bool(ok)


def evaluate_divergence_alert(index: GraphIndex, alert_node_id: str) -> GateResult:
    """Walks divergence -> proposal -> complication -> root error.

    Pure and synchronous: takes an already-fetched graph index and returns a
    verdict. No I/O, so the decision is reproducible from a snapshot and can be
    tested without a running case.
    """
    result = GateResult(passed=False)

    alert = index.nodes_by_id.get(alert_node_id)
    if not _check(result, "alert_exists", alert is not None, f"{alert_node_id} is not on the graph"):
        return result

    # --- divergence -> proposal ------------------------------------------
    proposal_id = alert.attrs.get("proposal_id")
    proposal = index.nodes_by_id.get(proposal_id) if proposal_id else None
    if not _check(
        result, "proposal_resolves", proposal is not None,
        f"divergence cites proposal {proposal_id!r} which is not on the graph",
    ):
        return result

    if not _check(
        result, "proposal_is_actionable", not proposal.attrs.get("escalated"),
        "the proposal was an escalation, so there is no corrective plan to have diverged from",
    ):
        return result

    if not _check(
        result, "proposal_has_provenance", bool(proposal.attrs.get("provenance")),
        "the corrective proposal carries no provenance, so its sourcing cannot be disclosed in the alert",
    ):
        return result

    if not _check(
        result, "proposal_has_steps", bool(proposal.attrs.get("steps")),
        "the corrective proposal has no steps, so there is nothing concrete to report",
    ):
        return result

    # --- proposal -> complication ----------------------------------------
    complication_id = proposal.attrs.get("complication_id")
    complication = index.nodes_by_id.get(complication_id) if complication_id else None
    if not _check(
        result, "complication_resolves", complication is not None,
        f"proposal cites complication {complication_id!r} which is not on the graph",
    ):
        return result

    # The check this gate exists for. A complication the reasoning step itself
    # marked unsupported must not become an external clinical alert — the whole
    # point of recording evidence_backed honestly upstream is that something
    # downstream refuses to act on it.
    if not _check(
        result, "complication_is_evidence_backed", bool(complication.attrs.get("evidence_backed")),
        "the complication is not literature-grounded; the reasoning step could not tie it to retrieved evidence",
    ):
        return result

    confidence = complication.attrs.get("confidence")
    if not _check(
        result, "complication_confidence", isinstance(confidence, (int, float)) and confidence >= MIN_COMPLICATION_CONFIDENCE,
        f"complication confidence {confidence} is below the {MIN_COMPLICATION_CONFIDENCE} floor",
    ):
        return result

    # --- complication -> root error ---------------------------------------
    root_error_id = complication.attrs.get("root_error_id")
    root_error = index.nodes_by_id.get(root_error_id) if root_error_id else None
    if not _check(
        result, "root_error_resolves", root_error is not None,
        f"complication cites root error {root_error_id!r} which is not on the graph",
    ):
        return result

    # --- the causal edges actually exist ----------------------------------
    # Attributes could be right while the graph is not. A viewer follows edges,
    # so an alert whose chain is only assertable via attrs is not traceable.
    has_causal = any(
        e.edge_kind == "causal_reasoning" and e.target_node_id == complication.node_id
        for e in index.edges_from.get(root_error.node_id, [])
    )
    if not _check(
        result, "causal_edge_exists", has_causal,
        "no causal_reasoning edge links the root error to the complication",
    ):
        return result

    has_proposal_edge = any(
        e.edge_kind == "proposal" and e.target_node_id == proposal.node_id
        for e in index.edges_from.get(complication.node_id, [])
    )
    if not _check(
        result, "proposal_edge_exists", has_proposal_edge,
        "no proposal edge links the complication to the corrective trajectory",
    ):
        return result

    # --- the divergence itself ---------------------------------------------
    div_confidence = alert.attrs.get("confidence")
    if not _check(
        result, "divergence_confidence", isinstance(div_confidence, (int, float)) and div_confidence >= MIN_DIVERGENCE_CONFIDENCE,
        f"divergence confidence {div_confidence} is below the {MIN_DIVERGENCE_CONFIDENCE} floor",
    ):
        return result

    if not _check(
        result, "not_advisory", not alert.attrs.get("advisory"),
        "the underlying proposal was acknowledged, so this divergence is advisory and must not reach an external channel",
    ):
        return result

    result.passed = True
    return result


def evaluate_documentation(index: GraphIndex, documentation_node_id: str) -> GateResult:
    """Gates a documentation write to the clinical record.

    A different chain from a divergence alert, so a different check set. What
    makes a note safe to file is not what makes an alert safe to raise: an
    alert must be grounded in evidence, whereas a note is a record of what the
    system observed and reasoned — including the parts it could not ground.
    Requiring evidence here would block honest documentation of an uncertain
    case, which is the opposite of what a record is for.

    What it does require is that a human approved it, that it says something,
    and that a reader can tell how much to trust it.
    """
    result = GateResult(passed=False)

    doc = index.nodes_by_id.get(documentation_node_id)
    if not _check(result, "draft_exists", doc is not None, f"{documentation_node_id} is not on the graph"):
        return result

    sections = doc.attrs.get("sections") or {}
    if not _check(result, "draft_has_content", bool(sections.get("summary", "").strip()), "the draft has no summary"):
        return result

    populated = sum(1 for k, v in sections.items() if isinstance(v, str) and v.strip())
    if not _check(
        result, "draft_is_substantive", populated >= 4,
        f"only {populated} sections are populated; this is too thin to file as a record",
    ):
        return result

    # The whole point of HITL #2. An unapproved note must never reach the
    # clinical record, whatever else is true of it.
    if not _check(
        result, "surgeon_approved", doc.attrs.get("approval_status") == "approved",
        f"approval_status is {doc.attrs.get('approval_status')!r}, not 'approved'",
    ):
        return result

    # A "case_is_benchmarked" check used to sit here — self-benchmarking
    # (agents/benchmark/agent.py) is disabled as a functional step (kept,
    # not deleted, in case it's wanted again later), so this graph would
    # never carry a benchmark node again and the check would fail-closed on
    # every single case, permanently. Removed rather than left to block
    # everything; re-add alongside benchmark_case if it's re-enabled.
    if not _check(
        result, "limitations_stated", bool(sections.get("limitations")),
        "the draft states no limitations, so a reader cannot tell what it does not cover",
    ):
        return result

    result.passed = True
    return result


async def verify_documentation(case_id: str, action_intent_id: str, documentation_node_id: str) -> GateResult:
    """Evaluates a documentation write and records the outcome. Same
    fail-closed contract as the alert path: an exception blocks."""
    try:
        index = GraphIndex(await get_state_snapshot(case_id))
        result = evaluate_documentation(index, documentation_node_id)
    except Exception as exc:
        logger.exception("verification[%s]: documentation evaluation failed — blocking", case_id)
        result = GateResult(passed=False, block_reasons=[f"verification could not be completed: {type(exc).__name__}"])

    await _write_outcome(case_id, action_intent_id, documentation_node_id, result)
    return result


async def verify(case_id: str, action_intent_id: str, alert_node_id: str) -> GateResult:
    """Evaluates a proposed external write and records the outcome on the graph.

    Returns the verdict. Deliberately does NOT perform the write on a pass —
    the caller does, so the gate can never be the thing that acts.
    """
    try:
        index = GraphIndex(await get_state_snapshot(case_id))
        result = evaluate_divergence_alert(index, alert_node_id)
    except Exception as exc:
        # Fail closed. An exception mid-evaluation means the chain could not be
        # verified, which is a block, never a pass.
        logger.exception("verification[%s]: evaluation failed for %s — blocking", case_id, alert_node_id)
        result = GateResult(passed=False, block_reasons=[f"verification could not be completed: {type(exc).__name__}"])

    await _write_outcome(case_id, action_intent_id, alert_node_id, result)
    logger.info(
        "verification[%s]: %s for %s — %s",
        case_id,
        "PASS" if result.passed else "BLOCK",
        action_intent_id,
        result.summary,
    )
    return result


async def _write_outcome(case_id: str, action_intent_id: str, subject_node_id: str, result: GateResult) -> None:
    """Records the verdict on the graph. Passes are recorded too — an approved
    external write with no record of having been checked is as opaque as an
    unexplained block."""
    node_id = node_ids.verification_block(action_intent_id)
    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=node_id,
                    node_type="verification_block",
                    label=("Verified — external write permitted" if result.passed else f"BLOCKED — {result.block_reasons[0][:70]}"),
                    attrs={
                        "passed": result.passed,
                        "checks": result.checks,
                        "block_reasons": result.block_reasons,
                        "action_intent_id": action_intent_id,
                        "subject_node_id": subject_node_id,
                    },
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                result.summary,
            ),
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(action_intent_id, node_id, "verification"),
                    source_node_id=action_intent_id,
                    target_node_id=node_id,
                    edge_kind="verification",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                    reason=result.summary,
                ),
                result.summary,
            ),
        ],
    )
