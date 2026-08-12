"""FHIR DocumentReference create + readback verification.

Non-negotiable #3 (context doc §3): every write must be followed by a
readback that confirms the server actually stored what we sent — a create
that returns 200/201 is not itself proof of a correct write (MedAgentBench
pattern). `write_document_reference` always performs both steps and reports
`verified=False` rather than raising if the readback doesn't match, so
callers (Action Router) can route to human-escalate instead of silently
trusting an unverified write.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

import requests
from pydantic import BaseModel

FHIR_BASE_URL = os.environ.get("FHIR_BASE_URL", "https://hapi.fhir.org/baseR4")
_REQUEST_TIMEOUT_S = 30

# hapi.fhir.org's public search index was found (Day 1 spike) to reliably
# index a resource's FIRST identifier but not consistently index additional
# identifiers in the same array — a same-process retry using
# find_existing_write() alone produced two DocumentReferences for one
# idempotency key instead of deduplicating. This process-local cache is the
# primary idempotency guard; remote search is kept only as a best-effort
# secondary check for cross-process retries (e.g. after a redeploy).
_local_idempotency_cache: dict[str, str] = {}


class DocumentReferenceWriteResult(BaseModel):
    resource_id: str
    version_id: str
    idempotency_key: str
    verified: bool
    mismatch_reason: str | None = None
    case_id: str
    source_agent: str
    timestamp: datetime
    raw_created: dict[str, Any]
    raw_readback: dict[str, Any] | None = None


def build_document_reference(
    case_id: str,
    *,
    idempotency_key: str,
    title: str,
    description: str,
    content_json_base64: str,
    loinc_code: str = "11504-8",
    loinc_display: str = "Surgical operation note",
) -> dict[str, Any]:
    """Builds a DocumentReference payload. `idempotency_key` is stamped into
    `identifier` so retried writes can be detected instead of duplicated
    (see test_failure_injection.py::test_retry_is_idempotent)."""
    return {
        "resourceType": "DocumentReference",
        "status": "current",
        "type": {"coding": [{"system": "http://loinc.org", "code": loinc_code, "display": loinc_display}]},
        "identifier": [
            {"system": "https://surggraph.dev/case-id", "value": case_id},
            {"system": "https://surggraph.dev/idempotency-key", "value": idempotency_key},
        ],
        "date": datetime.now(timezone.utc).isoformat(),
        "content": [
            {
                "attachment": {
                    "contentType": "application/json",
                    "data": content_json_base64,
                    "title": title,
                }
            }
        ],
        "description": description,
    }


def find_existing_write(case_id: str, idempotency_key: str) -> dict[str, Any] | None:
    """Searches for a prior DocumentReference with this idempotency key
    before writing again, so a retried write never creates a duplicate."""
    resp = requests.get(
        f"{FHIR_BASE_URL}/DocumentReference",
        params={"identifier": f"https://surggraph.dev/idempotency-key|{idempotency_key}"},
        headers={"Accept": "application/fhir+json"},
        timeout=_REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    bundle = resp.json()
    entries = bundle.get("entry", [])
    return entries[0]["resource"] if entries else None


def _fields_match(written: dict[str, Any], readback: dict[str, Any]) -> tuple[bool, str | None]:
    if readback.get("resourceType") != "DocumentReference":
        return False, "resourceType mismatch"
    if readback.get("status") != written["status"]:
        return False, "status mismatch"
    written_ids = {(i["system"], i["value"]) for i in written["identifier"]}
    readback_ids = {(i["system"], i["value"]) for i in readback.get("identifier", [])}
    if not written_ids.issubset(readback_ids):
        return False, "identifier mismatch"
    written_attachment = written["content"][0]["attachment"]
    readback_attachment = (readback.get("content") or [{}])[0].get("attachment", {})
    if readback_attachment.get("data") != written_attachment["data"]:
        return False, "attachment data mismatch"
    if readback.get("type", {}).get("coding", [{}])[0].get("code") != written["type"]["coding"][0]["code"]:
        return False, "type.coding mismatch"
    return True, None


def write_document_reference(
    case_id: str,
    *,
    title: str,
    description: str,
    content_json_base64: str,
    source_agent: str,
    idempotency_key: str | None = None,
) -> DocumentReferenceWriteResult:
    idempotency_key = idempotency_key or f"{case_id}-{uuid.uuid4().hex[:12]}"

    existing: dict[str, Any] | None = None
    cached_id = _local_idempotency_cache.get(idempotency_key)
    if cached_id is not None:
        resp = requests.get(
            f"{FHIR_BASE_URL}/DocumentReference/{cached_id}",
            headers={"Accept": "application/fhir+json"},
            timeout=_REQUEST_TIMEOUT_S,
        )
        if resp.ok:
            existing = resp.json()
    if existing is None:
        existing = find_existing_write(case_id, idempotency_key)  # best-effort; see module note

    if existing is not None:
        verified, mismatch_reason = _fields_match(
            build_document_reference(
                case_id,
                idempotency_key=idempotency_key,
                title=title,
                description=description,
                content_json_base64=content_json_base64,
            ),
            existing,
        )
        return DocumentReferenceWriteResult(
            resource_id=existing["id"],
            version_id=existing["meta"]["versionId"],
            idempotency_key=idempotency_key,
            verified=verified,
            mismatch_reason=mismatch_reason,
            case_id=case_id,
            source_agent=source_agent,
            timestamp=datetime.now(timezone.utc),
            raw_created=existing,
            raw_readback=existing,
        )

    payload = build_document_reference(
        case_id,
        idempotency_key=idempotency_key,
        title=title,
        description=description,
        content_json_base64=content_json_base64,
    )

    create_resp = requests.post(
        f"{FHIR_BASE_URL}/DocumentReference",
        json=payload,
        headers={"Content-Type": "application/fhir+json"},
        timeout=_REQUEST_TIMEOUT_S,
    )
    create_resp.raise_for_status()
    created = create_resp.json()
    resource_id = created["id"]
    version_id = created["meta"]["versionId"]

    read_resp = requests.get(
        f"{FHIR_BASE_URL}/DocumentReference/{resource_id}",
        headers={"Accept": "application/fhir+json"},
        timeout=_REQUEST_TIMEOUT_S,
    )
    read_resp.raise_for_status()
    readback = read_resp.json()

    verified, mismatch_reason = _fields_match(payload, readback)
    _local_idempotency_cache[idempotency_key] = resource_id

    return DocumentReferenceWriteResult(
        resource_id=resource_id,
        version_id=version_id,
        idempotency_key=idempotency_key,
        verified=verified,
        mismatch_reason=mismatch_reason,
        case_id=case_id,
        source_agent=source_agent,
        timestamp=datetime.now(timezone.utc),
        raw_created=created,
        raw_readback=readback,
    )
