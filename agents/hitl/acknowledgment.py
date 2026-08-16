"""HITL #1 — the surgeon's acknowledge/dismiss on a corrective proposal.

docs/agentic_workflow.md §11, docs/plan_v2 §6 step 7. The tiered-autonomy
pattern: the system proposes autonomously, and a human engaging with the
proposal changes what happens next.

    ACKNOWLEDGED  the proposal stays live and divergence detection keeps
                  running against it, but its divergences are marked advisory
                  and never reach an external channel. The surgeon has seen it;
                  paging them about it would be noise.

    DISMISSED     the proposal is out of play. Divergence detection stops
                  monitoring it entirely.

Acknowledgment SILENCES, it does not retire. That distinction is the reason
divergence detection keeps polling an acknowledged proposal — the case record
should still show that the surgeon departed from a plan they had seen, even
though nobody was paged about it.

THE HUMAN ACTION IS RECORDED AS A HUMAN ACTION. A separate `manual_event` node
with `source_agent="human"` is written alongside the attribute update, edged to
the proposal. Mutating the proposal silently would leave a graph where the
system appears to have decided on its own to stop alerting.

WHERE THIS LIVES, AND WHY IT DEVIATES FROM §11's WORDING. The docs say
`POST /events/manual` on the state service is the sole channel for HITL events.
That cannot work as written: interpreting `acknowledgment_outcome` is surgical
domain knowledge, and plan_v2 §4.3 requires the state service to know nothing
about surgery — only generic graph storage and change streaming. So the domain
handling lives here and is exposed by the orchestrator, which already owns the
case. The state service's generic manual-event endpoint is untouched and still
serves free-text injection.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch
from tools.state_tools import apply_state_patches, get_state_snapshot

logger = logging.getLogger(__name__)

SOURCE_AGENT = "human"
_SOURCE_TOOL = "hitl_acknowledgment"

AcknowledgmentOutcome = Literal["acknowledged", "dismissed"]


class ProposalNotFound(Exception):
    pass


class NotAProposal(Exception):
    pass


async def record_acknowledgment(
    case_id: str, proposal_node_id: str, outcome: AcknowledgmentOutcome
) -> dict:
    """Applies a surgeon's acknowledge/dismiss to a corrective proposal.

    Raises rather than silently no-oping on a bad target: a HITL action that
    appears to succeed while changing nothing is worse than an error, because
    the surgeon believes they have engaged with the proposal.
    """
    snapshot = await get_state_snapshot(case_id)
    proposal = next((n for n in snapshot.nodes if n.node_id == proposal_node_id), None)

    if proposal is None:
        raise ProposalNotFound(f"{proposal_node_id} is not on case {case_id}'s graph")
    if proposal.node_type != "corrective_trajectory":
        raise NotAProposal(f"{proposal_node_id} is a {proposal.node_type}, not a corrective proposal")

    acknowledged_at = datetime.now(timezone.utc).isoformat()
    event_node_id = node_ids.manual_event(uuid.uuid4().hex[:8])

    label = (
        "Surgeon acknowledged the proposal"
        if outcome == "acknowledged"
        else "Surgeon dismissed the proposal"
    )
    detail = (
        "Acknowledged — divergence monitoring continues in advisory mode; no external alert will be raised."
        if outcome == "acknowledged"
        else "Dismissed — divergence monitoring stops for this proposal."
    )

    await apply_state_patches(
        case_id,
        [
            # The proposal, updated in place. Divergence detection reads this
            # on its next poll — no bus subscription needed, since it re-fetches
            # the snapshot each time.
            (
                proposal.model_copy(
                    update={
                        "attrs": {
                            **proposal.attrs,
                            "acknowledgment_outcome": outcome,
                            "acknowledged_at": acknowledged_at,
                        }
                    }
                ),
                None,
                detail,
            ),
            # The human's action, recorded as the human's action.
            (
                GraphNodePatch(
                    node_id=event_node_id,
                    node_type="manual_event",
                    label=label,
                    attrs={
                        "event_kind": "hitl_acknowledgment",
                        "outcome": outcome,
                        "target_node_id": proposal_node_id,
                        "at": acknowledged_at,
                        "detail": detail,
                    },
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                detail,
            ),
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(event_node_id, proposal_node_id, "detection"),
                    source_node_id=event_node_id,
                    target_node_id=proposal_node_id,
                    edge_kind="detection",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                    reason=detail,
                ),
                detail,
            ),
        ],
    )

    logger.info("hitl[%s]: %s %s — %s", case_id, outcome, proposal_node_id, detail)
    return {
        "case_id": case_id,
        "proposal_node_id": proposal_node_id,
        "outcome": outcome,
        "acknowledged_at": acknowledged_at,
        "event_node_id": event_node_id,
        "detail": detail,
    }
