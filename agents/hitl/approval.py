"""HITL #2 — the surgeon approving, editing or rejecting the operative record.

docs/agentic_workflow.md §11, docs/plan_v2 §6 step 13-14.

NO PARKED COROUTINE. The docs describe the orchestrator entering an
`awaiting_approval` state after drafting, which read naturally as awaiting a
future inside the case's background task. That cannot work: approval may come
minutes or hours later, and a coroutine parked that long does not survive a
restart, a redeploy, or scale-to-zero — the case would silently lose its
pending note.

So there is no waiting state at all. The draft sits on the graph with
`approval_status: pending`, which is durable because the graph is, and this
endpoint does the whole remaining flow synchronously when the surgeon acts:
approve -> gate -> FHIR write -> outcome. Nothing is held open in between.

EDIT IS A REAL OUTCOME, NOT A CONVENIENCE. A surgeon correcting the draft
before filing is the expected case, not an edge case, and the edited text is
what gets written — with the original preserved alongside so the record shows
what the system produced versus what the clinician signed off.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

from agents.verification_gate import gate
from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch
from tools.fhir_write import write_document_reference
from tools.state_tools import apply_state_patches, get_state_snapshot

logger = logging.getLogger(__name__)

SOURCE_AGENT = "human"
_SOURCE_TOOL = "hitl_documentation_approval"

ApprovalOutcome = Literal["approved", "rejected", "edited"]


class DraftNotFound(Exception):
    pass


async def record_approval(
    case_id: str, outcome: ApprovalOutcome, edited_sections: dict | None = None
) -> dict:
    """Applies the surgeon's decision and, on approval, files the record.

    Returns what actually happened at each stage rather than a bare success
    flag — approval, gate verdict, and delivery are three separate things that
    can each go differently, and collapsing them would hide which one failed.
    """
    snapshot = await get_state_snapshot(case_id)
    doc_node_id = node_ids.documentation(case_id)
    doc = next((n for n in snapshot.nodes if n.node_id == doc_node_id), None)
    if doc is None:
        raise DraftNotFound(f"no operative record draft on case {case_id}")

    decided_at = datetime.now(timezone.utc).isoformat()
    sections = dict(doc.attrs.get("sections") or {})
    original_sections = sections

    if outcome == "edited":
        if not edited_sections:
            raise ValueError("outcome 'edited' requires edited_sections")
        sections = {**sections, **edited_sections}

    # An edit is an approval of the edited text — the surgeon has signed off on
    # what they wrote. Treating it as a separate un-filed state would leave a
    # corrected note sitting unfiled for no reason.
    approved = outcome in ("approved", "edited")
    status = "approved" if approved else "rejected"

    await apply_state_patches(
        case_id,
        [
            (
                doc.model_copy(
                    update={
                        "label": (
                            "Operative record — surgeon approved"
                            if approved
                            else "Operative record — surgeon rejected"
                        ),
                        "attrs": {
                            **doc.attrs,
                            "approval_status": status,
                            "approval_outcome": outcome,
                            "surgeon_reviewed": True,
                            "decided_at": decided_at,
                            "sections": sections,
                            # Kept so the record shows what the system produced
                            # versus what the clinician signed off on.
                            **({"original_sections": original_sections} if outcome == "edited" else {}),
                        },
                    }
                ),
                None,
                f"Surgeon {outcome} the operative record",
            ),
            *_human_action_patches(case_id, doc_node_id, outcome, decided_at),
        ],
    )
    logger.info("hitl[%s]: operative record %s", case_id, outcome)

    if not approved:
        return {"case_id": case_id, "outcome": outcome, "filed": False, "detail": "rejected; nothing written"}

    return await _file_record(case_id, doc_node_id, sections)


def _human_action_patches(case_id: str, doc_node_id: str, outcome: str, at: str) -> list[tuple]:
    """The node AND its edge to the draft.

    An earlier version returned only the node, which left every surgeon
    approval floating unconnected on the graph — the chain validator caught it.
    A human decision with no link to what it decided about is unreadable.
    """
    event_id = node_ids.manual_event(uuid.uuid4().hex[:8])
    label = f"Surgeon {outcome} the operative record"
    return [
        (
            GraphNodePatch(
                node_id=event_id,
                node_type="manual_event",
                label=label,
                attrs={"event_kind": "hitl_documentation_approval", "outcome": outcome, "target_node_id": doc_node_id, "at": at},
                source_agent=SOURCE_AGENT,
                source_tool=_SOURCE_TOOL,
            ),
            None,
            label,
        ),
        (
            None,
            GraphEdgePatch(
                edge_id=node_ids.edge(event_id, doc_node_id, "detection"),
                source_node_id=event_id,
                target_node_id=doc_node_id,
                edge_kind="detection",
                source_agent=SOURCE_AGENT,
                source_tool=_SOURCE_TOOL,
                reason=label,
            ),
            label,
        ),
    ]


async def _file_record(case_id: str, doc_node_id: str, sections: dict) -> dict:
    """Gate, then write. Same fail-closed contract as the alert path."""
    intent_id = node_ids.action_intent("documentation", uuid.uuid4().hex[:10])

    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=intent_id,
                    node_type="action_intent",
                    label="Proposed clinical write: operative record",
                    attrs={
                        "kind": "documentation",
                        "destination": "fhir_document_reference",
                        "documentation_node_id": doc_node_id,
                        "status": "awaiting_verification",
                    },
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                "Operative record approved, pending verification",
            ),
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(doc_node_id, intent_id, "detection"),
                    source_node_id=doc_node_id,
                    target_node_id=intent_id,
                    edge_kind="detection",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                    reason="This approved record proposed a clinical write",
                ),
                "This approved record proposed a clinical write",
            ),
        ],
    )

    verdict = await gate.verify_documentation(case_id, intent_id, doc_node_id)
    if not verdict.passed:
        await _write_outcome(case_id, intent_id, False, f"Blocked by verification gate — {verdict.summary}", blocked=True)
        return {"case_id": case_id, "outcome": "approved", "filed": False, "detail": verdict.summary}

    try:
        result = write_document_reference(
            case_id,
            title=f"SurgGraph operative record — {case_id}",
            description=sections.get("summary", "")[:400],
            content_json_base64=base64.b64encode(json.dumps(sections, indent=2).encode()).decode(),
            source_agent=SOURCE_AGENT,
            idempotency_key=f"{case_id}-operative-record",
        )
    except Exception as exc:
        await _write_outcome(case_id, intent_id, False, f"FHIR write failed: {type(exc).__name__}: {exc}")
        return {"case_id": case_id, "outcome": "approved", "filed": False, "detail": str(exc)}

    verified = getattr(result, "verified", False)
    resource_id = getattr(result, "resource_id", None)
    url = f"{__import__('tools.fhir_write', fromlist=['FHIR_BASE_URL']).FHIR_BASE_URL}/DocumentReference/{resource_id}" if resource_id else None
    detail = f"filed as {url}" + ("" if verified else " (readback did not confirm)")

    await _write_outcome(case_id, intent_id, True, detail, resource_url=url, readback_verified=verified)
    return {"case_id": case_id, "outcome": "approved", "filed": True, "detail": detail, "resource_url": url}


async def _write_outcome(
    case_id: str,
    intent_id: str,
    filed: bool,
    detail: str,
    resource_url: str | None = None,
    readback_verified: bool = False,
    blocked: bool = False,
) -> None:
    node_id = node_ids.action_outcome(intent_id)
    label = (
        "Operative record suppressed by verification gate"
        if blocked
        else ("Operative record filed" + ("" if readback_verified else " (readback unconfirmed)"))
        if filed
        else "Operative record FAILED to file"
    )
    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=node_id,
                    node_type="action_outcome",
                    label=label,
                    attrs={
                        "delivered": filed,
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
    logger.info("hitl[%s]: %s — %s", case_id, label, detail)
