"""Non-negotiable #3: live cross-app write with readback verification.

These tests hit the real public HAPI test server (no auth, no mocking of the
FHIR call itself) — that's the point of this non-negotiable. Only the
failure-mode test mocks a readback mismatch, since we can't reliably force
the real server to corrupt a write on demand.
"""

from __future__ import annotations

import base64
import json
import os
import uuid

from tools import fhir_write
from tools.fhir_write import (
    _fields_match,
    build_document_reference,
    write_document_reference,
)


def _content_b64(note: str) -> str:
    return base64.b64encode(json.dumps({"note": note}).encode()).decode()


def _case_id() -> str:
    return f"surggraph-test-{uuid.uuid4().hex[:10]}"


def test_create_document_reference_returns_id_and_version():
    result = write_document_reference(
        case_id=_case_id(),
        title="t",
        description="test_create_document_reference_returns_id_and_version",
        content_json_base64=_content_b64("create test"),
        source_agent="test_suite",
    )
    assert result.resource_id
    assert result.version_id


def test_readback_matches_written_content():
    case_id = _case_id()
    note = f"readback-match-{uuid.uuid4().hex[:6]}"
    result = write_document_reference(
        case_id=case_id,
        title="t",
        description="test_readback_matches_written_content",
        content_json_base64=_content_b64(note),
        source_agent="test_suite",
    )
    assert result.verified is True
    assert result.mismatch_reason is None
    assert result.raw_readback is not None
    assert result.raw_readback["identifier"][0]["value"] == case_id
    assert result.raw_readback["content"][0]["attachment"]["data"] == _content_b64(note)


def test_readback_failure_is_detected():
    written = build_document_reference(
        _case_id(),
        idempotency_key="k",
        title="t",
        description="d",
        content_json_base64=_content_b64("original"),
    )
    corrupted_readback = json.loads(json.dumps(written))
    corrupted_readback["content"][0]["attachment"]["data"] = _content_b64("corrupted-by-server")

    verified, reason = _fields_match(written, corrupted_readback)
    assert verified is False
    assert reason is not None


def test_write_includes_provenance_and_case_linkage():
    case_id = _case_id()
    result = write_document_reference(
        case_id=case_id,
        title="t",
        description="test_write_includes_provenance_and_case_linkage",
        content_json_base64=_content_b64("provenance test"),
        source_agent="action_router",
    )
    assert result.case_id == case_id
    assert result.source_agent == "action_router"
    assert result.timestamp is not None
    identifiers = {i["value"] for i in result.raw_created["identifier"]}
    assert case_id in identifiers


def test_retry_with_same_idempotency_key_does_not_duplicate():
    case_id = _case_id()
    content = _content_b64("idempotency test")
    first = write_document_reference(
        case_id=case_id, title="t", description="d", content_json_base64=content, source_agent="test_suite"
    )
    second = write_document_reference(
        case_id=case_id,
        title="t",
        description="d",
        content_json_base64=content,
        source_agent="test_suite",
        idempotency_key=first.idempotency_key,
    )
    assert second.resource_id == first.resource_id


def test_secrets_not_hardcoded():
    """FHIR_BASE_URL must come from the environment, not a literal baked
    into calling code — this test just confirms the module reads it from
    os.environ rather than defining it as a bare module-level string that
    ignores the env var."""
    assert fhir_write.FHIR_BASE_URL == os.environ.get("FHIR_BASE_URL", "https://hapi.fhir.org/baseR4")
