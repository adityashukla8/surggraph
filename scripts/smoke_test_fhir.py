"""Spike A: prove FHIR create + readback works end-to-end against the public
HAPI test server before anything else in the build depends on it.

Usage: uv run scripts/smoke_test_fhir.py
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

import requests

FHIR_BASE_URL = os.environ.get("FHIR_BASE_URL", "https://hapi.fhir.org/baseR4")


def build_document_reference(case_id: str) -> dict:
    return {
        "resourceType": "DocumentReference",
        "status": "current",
        "type": {
            "coding": [
                {
                    "system": "http://loinc.org",
                    "code": "11504-8",
                    "display": "Surgical operation note",
                }
            ]
        },
        "identifier": [
            {
                "system": "https://surggraph.dev/case-id",
                "value": case_id,
            }
        ],
        "date": datetime.now(timezone.utc).isoformat(),
        "content": [
            {
                "attachment": {
                    "contentType": "application/json",
                    "data": "eyJzbW9rZV90ZXN0IjogdHJ1ZX0=",  # base64: {"smoke_test": true}
                    "title": "SurgGraph smoke test artifact",
                }
            }
        ],
        "description": "SurgGraph Spike A smoke test — safe to delete.",
    }


def main() -> int:
    case_id = f"surggraph-smoketest-{uuid.uuid4().hex[:8]}"
    payload = build_document_reference(case_id)

    print(f"[1/3] POST {FHIR_BASE_URL}/DocumentReference (case_id={case_id})")
    create_resp = requests.post(
        f"{FHIR_BASE_URL}/DocumentReference",
        json=payload,
        headers={"Content-Type": "application/fhir+json"},
        timeout=30,
    )
    create_resp.raise_for_status()
    created = create_resp.json()
    resource_id = created["id"]
    version_id = created["meta"]["versionId"]
    print(f"      -> created id={resource_id} versionId={version_id}")

    print(f"[2/3] GET {FHIR_BASE_URL}/DocumentReference/{resource_id}")
    read_resp = requests.get(
        f"{FHIR_BASE_URL}/DocumentReference/{resource_id}",
        headers={"Accept": "application/fhir+json"},
        timeout=30,
    )
    read_resp.raise_for_status()
    readback = read_resp.json()

    print("[3/3] Verifying readback content matches what was written...")
    assert readback["resourceType"] == "DocumentReference"
    assert readback["identifier"][0]["value"] == case_id
    assert readback["status"] == payload["status"]
    assert readback["content"][0]["attachment"]["data"] == payload["content"][0]["attachment"]["data"]
    assert readback["type"]["coding"][0]["code"] == payload["type"]["coding"][0]["code"]

    print("\nSPIKE A PASSED: FHIR create + readback verified against", FHIR_BASE_URL)
    print(f"  resource: DocumentReference/{resource_id} (version {version_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
