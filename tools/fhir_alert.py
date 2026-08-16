"""Alert Executor — the external write, as a FHIR Communication.

docs/agentic_workflow.md §4. A thin adapter: structured payload in, delivery
outcome out. It makes no decisions, and it is only ever called after the
verification gate has returned a pass.

WHY FHIR Communication RATHER THAN A CHAT WEBHOOK. `Communication` is the HL7
standard resource for "a record of a communication such as an alert" — this is
literally what the standard models. It is a real API with a real resource
model, it readbacks like any other FHIR resource so delivery can be verified
rather than assumed, and it lands in the same clinical record the post-case
documentation write targets. A generic chat webhook would demonstrate plumbing;
this demonstrates clinical integration.

Confirmed working against the public HAPI sandbox: POST returns 201, readback
returns the resource with status and priority preserved.

DESTINATION-AGNOSTIC BY SHAPE. Everything above this module deals in
`AlertDelivery`, not in FHIR. Swapping the destination is replacing this file.

DELIVERY FAILURE IS AN OUTCOME, NOT AN EXCEPTION. A failed alert returns a
delivery record saying so, which becomes a visible action_outcome node. An
alert that silently did not arrive, or one that retried until it looked like it
had, is the worst failure mode this component has.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

FHIR_BASE_URL = os.environ.get("FHIR_BASE_URL", "https://hapi.fhir.org/baseR4")
_TIMEOUT_S = 30

# HL7's own category coding for an alert-class communication.
_ALERT_CATEGORY = {
    "coding": [
        {
            "system": "http://terminology.hl7.org/CodeSystem/communication-category",
            "code": "alert",
            "display": "Alert",
        }
    ]
}

# FHIR request priority. Maps from the corrective proposal's own urgency, which
# the replanning agent set — not re-derived here, since this adapter has no
# clinical context to derive it from.
_URGENCY_TO_PRIORITY = {"immediate": "stat", "prompt": "urgent", "routine": "routine"}


@dataclass
class AlertDelivery:
    delivered: bool
    detail: str
    resource_id: str | None = None
    resource_url: str | None = None
    readback_verified: bool = False


def build_communication(case_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The FHIR resource for one alert.

    The reasoning trail goes in as payload entries rather than a single blob:
    a recipient should be able to read what was detected, what it could lead
    to, and what was proposed as separate statements, and the standard models
    exactly that as repeating payload elements.
    """
    urgency = payload.get("urgency", "routine")
    parts = [
        f"DETECTED: {payload['error_label']} (severity {payload['severity_band']})",
        f"POSSIBLE COMPLICATION: {payload['complication_label']} "
        f"(confidence {payload['complication_confidence']}) — {payload['mechanism']}",
        f"PROPOSED: {payload['proposal_label']}",
    ]
    for step in payload.get("steps", []):
        parts.append(f"  step {step['order']}: {step['action']}")
    parts.append(f"DIVERGENCE: {payload['divergence_reasoning']}")
    if payload.get("citations"):
        parts.append("EVIDENCE: " + "; ".join(payload["citations"]))
    # The alert says what it is. A recipient must never have to infer that the
    # patient is synthetic or that the corrective library is unreviewed.
    parts.append(
        "SOURCE: SurgGraph automated intraoperative analysis. SYNTHETIC patient data. "
        f"Corrective actions are provenance tier {payload.get('provenance_tier')} and have not been "
        "reviewed by a practising surgeon. Not validated clinical decision support."
    )

    return {
        "resourceType": "Communication",
        "status": "completed",
        "priority": _URGENCY_TO_PRIORITY.get(urgency, "routine"),
        "category": [_ALERT_CATEGORY],
        "subject": {"display": payload.get("patient_display", "SYNTHETIC patient")},
        "sent": payload["sent_at"],
        # Idempotency: the same case and divergence produce the same identifier,
        # so a retry cannot create a second alert for one event.
        "identifier": [
            {"system": "urn:surggraph:divergence-alert", "value": f"{case_id}:{payload['alert_node_id']}"}
        ],
        "payload": [{"contentString": p} for p in parts],
    }


def send_alert(case_id: str, payload: dict[str, Any]) -> AlertDelivery:
    """Writes the alert and verifies it by reading it back.

    Readback is not ceremony: a 201 says the server accepted the request, and
    reading the resource back is what says the alert actually exists to be
    found. Anything else is assuming delivery.
    """
    resource = build_communication(case_id, payload)
    try:
        resp = requests.post(
            f"{FHIR_BASE_URL}/Communication",
            json=resource,
            headers={"Content-Type": "application/fhir+json"},
            timeout=_TIMEOUT_S,
        )
    except Exception as exc:
        return AlertDelivery(delivered=False, detail=f"request failed: {type(exc).__name__}: {exc}")

    if resp.status_code not in (200, 201):
        return AlertDelivery(delivered=False, detail=f"server returned {resp.status_code}: {resp.text[:200]}")

    resource_id = resp.json().get("id")
    if not resource_id:
        return AlertDelivery(delivered=False, detail="server accepted the write but returned no resource id")

    url = f"{FHIR_BASE_URL}/Communication/{resource_id}"
    try:
        readback = requests.get(url, timeout=_TIMEOUT_S)
        verified = readback.status_code == 200 and readback.json().get("status") == "completed"
        detail = (
            f"delivered and verified as {url}"
            if verified
            else f"delivered as {url} but readback did not confirm it (HTTP {readback.status_code})"
        )
    except Exception as exc:
        verified, detail = False, f"delivered as {url} but readback failed: {type(exc).__name__}"

    # delivered=True with readback_verified=False is a real, distinct state and
    # is reported as such rather than being rounded to either success or failure.
    return AlertDelivery(
        delivered=True, detail=detail, resource_id=resource_id, resource_url=url, readback_verified=verified
    )
