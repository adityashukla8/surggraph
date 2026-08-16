"""Alert Routing Agent — event-driven off divergence alerts.

docs/agentic_workflow.md §3 agent 8, docs/plan_v2 §6 step 8.

    divergence_alert
        -> assemble the whole reasoning trail into a structured payload
        -> write an action_intent node (the pending external write, visible
           BEFORE anything leaves the system)
        -> call the verification gate synchronously
        -> on pass, and only on pass, hand to the executor
        -> record the real delivery outcome

THE INTENT NODE EXISTS BEFORE THE WRITE. A proposed external action is on the
graph, and gated, before it can happen — so a blocked alert leaves a visible
record of what would have been sent and why it was not, rather than leaving no
trace at all.

THIS AGENT DOES NOT DECIDE WHETHER TO SEND. It decides what the message would
say. The gate decides whether it goes. Keeping those in separate modules is
what stops the component that wants to alert from also being the component that
approves alerting.

NO REASONING CALL. Every field in the payload is already on the graph, written
by the agent that reasoned it. Re-generating that text here would give a second
model the chance to paraphrase a clinical claim into something its author never
said — the assembly is deliberately mechanical.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from agents.verification_gate import gate
from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch, StateDiffEvent
from tools.context_slice import GraphIndex
from tools.fhir_alert import send_alert
from tools.state_tools import apply_state_patches, get_state_snapshot

logger = logging.getLogger(__name__)

SOURCE_AGENT = "alert_routing"
_SOURCE_TOOL = "route_alert"

_routed: set[tuple[str, str]] = set()


def _assemble_payload(index: GraphIndex, alert) -> dict | None:
    """Pulls the trail off the graph. Returns None if it does not resolve —
    the gate would block that anyway, and building a half-payload first would
    only produce a misleading intent node."""
    proposal = index.nodes_by_id.get(alert.attrs.get("proposal_id", ""))
    if proposal is None:
        return None
    complication = index.nodes_by_id.get(proposal.attrs.get("complication_id", ""))
    if complication is None:
        return None
    root_error = index.nodes_by_id.get(complication.attrs.get("root_error_id", ""))
    if root_error is None:
        return None

    # Only papers that genuinely support the complication — the `evidence`
    # edges — not everything the retrieval happened to return.
    citations = [
        f"{n.label} ({n.attrs.get('journal')} {n.attrs.get('year')}, {n.attrs.get('url')})"
        for n in index.neighbors_in(complication.node_id, edge_kind="evidence")
    ]

    twin = index.snapshot_slot(node_ids.patient_twin()) or {}

    return {
        "alert_node_id": alert.node_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "patient_display": (twin.get("label") or "SYNTHETIC patient"),
        "error_label": root_error.label,
        "severity_band": root_error.attrs.get("severity_band"),
        "complication_label": complication.label,
        "complication_confidence": complication.attrs.get("confidence"),
        "mechanism": complication.attrs.get("mechanism", ""),
        "proposal_label": proposal.label,
        "steps": proposal.attrs.get("steps", []),
        "urgency": proposal.attrs.get("urgency", "routine"),
        "divergence_reasoning": alert.attrs.get("reasoning", ""),
        "citations": citations,
        "provenance_tier": (proposal.attrs.get("provenance") or {}).get("tier"),
    }


async def route_alert(case_id: str, alert_node_id: str) -> str | None:
    """Routes one divergence alert. Returns the action_intent node id."""
    if (case_id, alert_node_id) in _routed:
        return None
    _routed.add((case_id, alert_node_id))

    index = GraphIndex(await get_state_snapshot(case_id))
    alert = index.nodes_by_id.get(alert_node_id)
    if alert is None:
        return None

    # docs §11: an acknowledged proposal still logs its divergences, but they
    # do not reach an external channel. Checked here as well as in the gate —
    # the gate is the guarantee, this avoids constructing an intent that exists
    # only to be blocked.
    if alert.attrs.get("advisory"):
        logger.info("alert_routing[%s]: %s is advisory, not routing", case_id, alert_node_id)
        return None

    payload = _assemble_payload(index, alert)
    intent_id = node_ids.action_intent("alert", uuid.uuid4().hex[:10])

    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=intent_id,
                    node_type="action_intent",
                    label=f"Proposed alert: {alert.label[:60]}",
                    attrs={
                        "kind": "alert",
                        "destination": "fhir_communication",
                        "alert_node_id": alert_node_id,
                        "payload": payload,
                        # Explicitly pending. Nothing has left the system yet,
                        # and the node says so until an outcome replaces it.
                        "status": "awaiting_verification",
                    },
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                "External alert proposed, pending verification",
            ),
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(alert_node_id, intent_id, "detection"),
                    source_node_id=alert_node_id,
                    target_node_id=intent_id,
                    edge_kind="detection",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                    reason="This divergence proposed an external alert",
                ),
                "This divergence proposed an external alert",
            ),
        ],
    )

    # Fail-closed gate. Synchronous on purpose: nothing may leave the system
    # while this is still undecided.
    verdict = await gate.verify(case_id, intent_id, alert_node_id)
    if not verdict.passed:
        await _record_outcome(
            case_id, intent_id, delivered=False, detail=f"Blocked by verification gate — {verdict.summary}", blocked=True
        )
        return intent_id

    if payload is None:
        # Defensive: the gate should already have blocked this.
        await _record_outcome(case_id, intent_id, delivered=False, detail="payload could not be assembled", blocked=True)
        return intent_id

    delivery = send_alert(case_id, payload)
    await _record_outcome(
        case_id,
        intent_id,
        delivered=delivery.delivered,
        detail=delivery.detail,
        resource_url=delivery.resource_url,
        readback_verified=delivery.readback_verified,
    )
    return intent_id


async def _record_outcome(
    case_id: str,
    intent_id: str,
    delivered: bool,
    detail: str,
    resource_url: str | None = None,
    readback_verified: bool = False,
    blocked: bool = False,
) -> None:
    """The real result of the attempt, whatever it was.

    A blocked alert and a failed alert both get an outcome node. An intent left
    with no outcome would be indistinguishable from one still in flight, which
    is exactly the ambiguity a record like this exists to remove.
    """
    node_id = node_ids.action_outcome(intent_id)
    if blocked:
        label = "Alert suppressed by verification gate"
    elif delivered:
        label = "Alert delivered" + ("" if readback_verified else " (readback unconfirmed)")
    else:
        label = "Alert FAILED to deliver"

    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=node_id,
                    node_type="action_outcome",
                    label=label,
                    attrs={
                        "delivered": delivered,
                        "blocked": blocked,
                        "readback_verified": readback_verified,
                        "detail": detail,
                        "resource_url": resource_url,
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
                    edge_id=node_ids.edge(intent_id, node_id, "outcome"),
                    source_node_id=intent_id,
                    target_node_id=node_id,
                    edge_kind="outcome",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                    reason=detail,
                ),
                detail,
            ),
        ],
    )
    logger.info("alert_routing[%s]: %s — %s", case_id, label, detail)


def subscribe(bus) -> None:
    async def handler(event: StateDiffEvent) -> None:
        if event.node is None:
            return
        await route_alert(event.case_id, event.node.node_id)

    bus.subscribe(SOURCE_AGENT, handler, node_types={"divergence_alert"})
