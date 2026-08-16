"""Trajectory Divergence Detection — docs/agentic_workflow.md §3 agent 7.

Activated by the appearance of a corrective proposal, not by a sweep: it polls
while that proposal is live and stops when it is resolved or the case moves on.
Zero writes while the surgeon is following the plan — an alert only exists
because something genuinely departed from it.

DETERMINISTIC FIRST, LLM SECOND. Two different kinds of question:

  DETERMINISTIC   Did the error this proposal addresses fire again after it was
                  made? That has a real answer in graph state, it is the
                  strongest available signal that a corrective did not take
                  effect, and it costs nothing. It also cannot be argued with,
                  which matters for something that will interrupt a surgeon.

  SEMANTIC        Is what the surgeon is doing consistent with the SPIRIT of
                  the plan? No amount of string comparison answers that, so it
                  escalates to a real reasoning call (subagent.py).

The deterministic pass runs every poll. The LLM only runs when the deterministic
signal is genuinely ambiguous, which keeps the common case free.

THIS IS NOT THE SAME SIGNAL AS AN ERROR. An error is "something went wrong". A
divergence is "the safer path we proposed is not being taken" — a different
claim, measured against a normative proposal rather than against correct
technique, and kept structurally separate throughout.

ADVISORY MODE. Once HITL #1 exists, an acknowledged proposal keeps being
monitored but its divergences are marked advisory and do not reach the alert
path (docs §11). Acknowledgment silences the alert; it does not retire the
proposal.
"""

from __future__ import annotations

import asyncio
import logging

from google.genai import types

from agents.divergence_detection.subagent import DivergenceJudgment, build_subagent
from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch, StateDiffEvent
from tools.adk_runner import run_llm_agent_once
from tools.context_slice import GraphIndex, divergence_detection as divergence_slice
from tools.state_tools import apply_state_patches, get_state_snapshot

logger = logging.getLogger(__name__)

SOURCE_AGENT = "divergence_detection"
_SOURCE_TOOL = "check_trajectory_divergence"

AGENT = build_subagent()

# docs §3 agent 7: "polls every 5-10s while a proposal is active".
POLL_INTERVAL_S = 8.0

# A proposal is not monitored forever. Past this many polls the surgical
# context has moved on far enough that comparing against it says more about
# staleness than about the surgeon.
MAX_POLLS = 12

# Below this many observations since the proposal, there is not enough evidence
# to judge anything. Perception is deliberately quiet, so a short list means a
# steady case, not a non-compliant one.
MIN_OBSERVATIONS_TO_JUDGE = 2

# One alert per proposal. Re-alerting on every poll for the same unchanged
# divergence is how an alert channel becomes noise a surgeon learns to ignore.
_alerted: set[tuple[str, str]] = set()

# And one alert per underlying EVENT, across proposals.
#
# Real finding from a live run: a single error spawned several complications,
# each of which produced its own corrective proposal, and each proposal then
# independently noticed the same recurrence — five near-identical alerts for
# one fact. Per-proposal deduplication cannot catch that, because each alert
# was genuinely the first for its own proposal.
#
# What a surgeon needs to hear is "you were asked to fix out-of-view and it
# happened again", once. How many complications happened to spawn a plan is
# an implementation detail of our reasoning, not information about the patient.
_alerted_evidence: set[tuple[str, str]] = set()


def _errors_after(index: GraphIndex, category: str, after_timestamp) -> list:
    """Errors of the same category written after the proposal was made.

    The strongest deterministic evidence a corrective did not take effect: the
    plan asked for a change and the same fault recurred anyway.
    """
    return [
        n
        for n in index.of_type("error")
        if n.attrs.get("error_category") == category and n.timestamp > after_timestamp
    ]


def _observations_after(index: GraphIndex, after_timestamp) -> list:
    return sorted(
        [n for n in index.of_type("perception_event") if n.timestamp > after_timestamp],
        key=lambda n: n.timestamp,
    )


def _format_for_judgment(proposal, observations: list) -> str:
    lines = [
        "PROPOSED CORRECTIVE PLAN",
        f"  {proposal.label}",
        f"  urgency: {proposal.attrs.get('urgency')}",
        "",
        "STEPS AND THEIR VERIFICATION CHECKS",
    ]
    for step in proposal.attrs.get("steps", []):
        lines.append(f"  {step['order']}. {step['action']}")
        lines.append(f"       verification check: {step['verification_check']}")

    lines += ["", "WHAT PERCEPTION ACTUALLY OBSERVED SINCE THE PROPOSAL"]
    if not observations:
        lines.append("  (nothing reported — perception only reports change, so this means a steady case)")
    for obs in observations:
        kind = obs.attrs.get("event_kind", "event")
        lines.append(f"  [{kind}] {obs.label}")
    return "\n".join(lines)


async def check_proposal(case_id: str, proposal_node_id: str) -> str | None:
    """One divergence check. Returns the alert node id if one was written."""
    if (case_id, proposal_node_id) in _alerted:
        return None

    index = GraphIndex(await get_state_snapshot(case_id))
    proposal = index.nodes_by_id.get(proposal_node_id)
    if proposal is None:
        return None

    # An escalation is not a plan, so there is nothing to diverge from.
    if proposal.attrs.get("escalated"):
        return None

    # docs §11: a dismissed proposal is out of play entirely. An ACKNOWLEDGED
    # one keeps being monitored, but silently.
    acknowledgment = proposal.attrs.get("acknowledgment_outcome")
    if acknowledgment == "dismissed":
        return None
    advisory = acknowledgment == "acknowledged"

    root_error = index.nodes_by_id.get(proposal.attrs.get("root_error_id", ""))
    category = root_error.attrs.get("error_category") if root_error else None

    observations = _observations_after(index, proposal.timestamp)
    recurrences = _errors_after(index, category, proposal.timestamp) if category else []

    # --- Deterministic pass ---------------------------------------------
    if recurrences:
        return await _write_alert(
            case_id,
            proposal,
            evidence_node_ids=[n.node_id for n in recurrences],
            method="deterministic",
            reasoning=(
                f"{category.replace('_', ' ')} recurred {len(recurrences)} time(s) after this corrective was "
                f"proposed — the plan asked for a change and the same fault happened again"
            ),
            confidence=0.9,
            advisory=advisory,
        )

    if len(observations) < MIN_OBSERVATIONS_TO_JUDGE:
        # Not enough happened to say anything. Deliberately not "aligned":
        # silence is not evidence of compliance either.
        return None

    # --- Semantic pass ----------------------------------------------------
    judgment: DivergenceJudgment = await run_llm_agent_once(
        AGENT,
        types.Content(role="user", parts=[types.Part(text=_format_for_judgment(proposal, observations))]),
        DivergenceJudgment,
        app_name="surggraph_divergence_detection",
    )

    if judgment.aligned is None:
        logger.info("divergence[%s]: cannot tell for %s — writing nothing", case_id, proposal_node_id)
        return None
    if judgment.aligned:
        logger.info("divergence[%s]: aligned with %s", case_id, proposal_node_id)
        return None

    return await _write_alert(
        case_id,
        proposal,
        evidence_node_ids=[o.node_id for o in observations[-3:]],
        method="semantic",
        reasoning=judgment.reasoning,
        confidence=judgment.confidence,
        advisory=advisory,
        unsatisfied_steps=judgment.unsatisfied_steps,
    )


def _evidence_already_alerted(case_id: str, evidence_node_ids: list[str]) -> bool:
    """True when every piece of evidence for this alert has already been
    reported. Partial overlap still alerts — that is genuinely new evidence."""
    return bool(evidence_node_ids) and all((case_id, e) in _alerted_evidence for e in evidence_node_ids)


async def _write_alert(
    case_id: str,
    proposal,
    evidence_node_ids: list[str],
    method: str,
    reasoning: str,
    confidence: float,
    advisory: bool,
    unsatisfied_steps: list[int] | None = None,
) -> str:
    if _evidence_already_alerted(case_id, evidence_node_ids):
        # Already reported via another proposal. Still mark this proposal as
        # handled so its monitor stops polling for something already said.
        _alerted.add((case_id, proposal.node_id))
        logger.info(
            "divergence[%s]: %s diverged on evidence already alerted — not repeating",
            case_id,
            proposal.node_id,
        )
        return ""

    window_index = len(_alerted)
    node_id = node_ids.divergence_alert(proposal.node_id, window_index)
    _alerted.add((case_id, proposal.node_id))
    for evidence_id in evidence_node_ids:
        _alerted_evidence.add((case_id, evidence_id))

    patches: list[tuple] = [
        (
            GraphNodePatch(
                node_id=node_id,
                node_type="divergence_alert",
                label=f"Diverging from: {proposal.label[:70]}",
                attrs={
                    "proposal_id": proposal.node_id,
                    # Which half of the design produced this, so a reviewer can
                    # tell a hard graph fact from a model's judgment.
                    "detection_method": method,
                    "confidence": confidence,
                    "reasoning": reasoning,
                    "unsatisfied_steps": unsatisfied_steps or [],
                    # docs §11: acknowledged proposals still log, they just do
                    # not reach the alert path.
                    "advisory": advisory,
                },
                source_agent=SOURCE_AGENT,
                source_tool=_SOURCE_TOOL,
            ),
            None,
            reasoning,
        ),
        # §4.2's trajectory-comparison edge: this alert exists because the
        # actual course was compared against that proposal.
        (
            None,
            GraphEdgePatch(
                edge_id=node_ids.edge(proposal.node_id, node_id, "trajectory_comparison"),
                source_node_id=proposal.node_id,
                target_node_id=node_id,
                edge_kind="trajectory_comparison",
                source_agent=SOURCE_AGENT,
                source_tool=_SOURCE_TOOL,
                reason=reasoning,
            ),
            reasoning,
        ),
    ]

    # The actual observations that evidenced the divergence, so the alert points
    # at real graph state rather than asserting a conclusion.
    for evidence_id in evidence_node_ids:
        patches.append(
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(evidence_id, node_id, "detection"),
                    source_node_id=evidence_id,
                    target_node_id=node_id,
                    edge_kind="detection",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                    reason="Observed evidence for this divergence",
                ),
                "Observed evidence for this divergence",
            )
        )

    await apply_state_patches(case_id, patches)
    logger.info(
        "divergence[%s]: ALERT (%s%s) on %s — %s",
        case_id,
        method,
        ", advisory" if advisory else "",
        proposal.node_id,
        reasoning,
    )
    return node_id


async def monitor_proposal(case_id: str, proposal_node_id: str) -> None:
    """Polls one proposal until it diverges, or the budget runs out.

    Bounded rather than open-ended: a proposal made early in a case stops being
    a meaningful yardstick once the surgery has moved on, and monitoring it
    forever would eventually compare the surgeon against an irrelevant plan.
    """
    for _ in range(MAX_POLLS):
        await asyncio.sleep(POLL_INTERVAL_S)
        try:
            result = await check_proposal(case_id, proposal_node_id)
            if result is not None:
                # A node id means an alert was written; an empty string means
                # this divergence was real but already reported elsewhere.
                # Either way this proposal is resolved and polling stops.
                return
        except Exception:
            # A failed check loses that check, not the monitor (docs §10).
            logger.exception("divergence[%s]: check failed for %s, continuing", case_id, proposal_node_id)
    logger.info("divergence[%s]: monitoring budget exhausted for %s", case_id, proposal_node_id)


def subscribe(bus) -> None:
    """Activated by the appearance of a proposal, per docs §3 agent 7 — the
    agent is dormant until there is something to diverge FROM."""

    async def handler(event: StateDiffEvent) -> None:
        if event.node is None or event.node.attrs.get("escalated"):
            return
        await monitor_proposal(event.case_id, event.node.node_id)

    bus.subscribe(SOURCE_AGENT, handler, node_types={"corrective_trajectory"})
