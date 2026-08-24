"""Model Armor screening for the operative record's outbound FHIR write.

Second fail-closed gate alongside the verification gate (agents/verification_
gate/gate.py) in agents/hitl/approval.py::_file_record — the gate checks the
CLAIM is evidenced; this checks the TEXT is safe to hand to an external
system. Both block the same way: write an action_outcome with blocked=True
and never call write_document_reference.

sanitize_model_response, not sanitize_user_prompt: the operative note is
Gemini-GENERATED content about to leave the system, not a user prompt about
to enter one. It's screened here because the note is synthesized from the
whole case graph, which includes literature abstracts pulled live from
PubMed/Semantic Scholar/Europe PMC (agents/literature_retrieval) — real
third-party text none of this project's own agents wrote, and the one
plausible route for something adversarial to ride into what gets filed.

FAILS CLOSED. Any API/transport error blocks the write rather than skipping
the check silently — an unscreened write is not a verified-safe write, the
same standard write_document_reference already holds itself to with its own
readback check.

Template `surggraph-fhir-outbound` (created via `gcloud model-armor
templates create`, not by this code — a security template is an operator
decision, not something an agent should be able to provision for itself)
enables: RAI filters (hate speech/harassment/dangerous/sexually explicit @
MEDIUM_AND_ABOVE), prompt-injection/jailbreak detection (@ HIGH confidence),
malicious URL detection, and Sensitive Data Protection in Basic mode (fixed
infotypes: credit cards, SSNs, financial accounts, ITINs, GCP credentials —
Advanced mode would need a separate Cloud DLP inspect template provisioned
first, a heavier lift not asked for here).
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import GoogleAPIError
from google.cloud import modelarmor_v1
from opentelemetry import trace
from pydantic import BaseModel

load_dotenv()

logger = logging.getLogger(__name__)

# A real span around the actual external call, not a log line pretending to
# be one — exported to Cloud Trace whenever tools/observability.py's
# telemetry is enabled (SURGGRAPH_ENABLE_CLOUD_TELEMETRY=true) via the
# already-configured global TracerProvider; a harmless no-op otherwise, so
# this is safe to leave in unconditionally rather than branching on the flag.
_tracer = trace.get_tracer(__name__)

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
LOCATION = os.environ.get("SURGGRAPH_REGION", "us-central1")
TEMPLATE_ID = os.environ.get("SURGGRAPH_MODEL_ARMOR_TEMPLATE_ID", "surggraph-fhir-outbound")

_TEMPLATE_NAME = f"projects/{PROJECT_ID}/locations/{LOCATION}/templates/{TEMPLATE_ID}"

_client: modelarmor_v1.ModelArmorClient | None = None


def _get_client() -> modelarmor_v1.ModelArmorClient:
    # Lazy singleton — same pattern as services/state_service/gcs_video.py's
    # Firestore client, not a new convention.
    global _client
    if _client is None:
        # Model Armor has no global endpoint for sanitize/template calls — a
        # request to the default global one comes back PERMISSION_DENIED
        # regardless of IAM, which reads exactly like an auth bug until you
        # know to look for it (confirmed live standing this template up).
        _client = modelarmor_v1.ModelArmorClient(
            client_options=ClientOptions(api_endpoint=f"modelarmor.{LOCATION}.rep.googleapis.com")
        )
    return _client


class ModelArmorScreenResult(BaseModel):
    blocked: bool
    reason: str | None = None
    raw_filter_match_state: str


def join_note_sections(sections: dict) -> str:
    """The one real definition of 'the note's screenable text' — shared by
    the draft-time screen (agents/documentation/agent.py, before a surgeon
    ever sees an Approve button) and the approval-time re-screen (agents/
    hitl/approval.py, which must re-check whatever the surgeon actually
    edited). Two call sites computing this slightly differently would mean
    two different things could each call themselves "the screened text"."""
    return "\n\n".join(str(v) for v in sections.values() if v)


def screen_operative_note(text: str) -> ModelArmorScreenResult:
    """Runs the operative note's plain text through sanitize_model_response.

    `text` must be the readable narrative (the join of the note's own
    section strings), never the base64 FHIR attachment payload — Model
    Armor's filters read natural language, not an encoded blob.
    """
    with _tracer.start_as_current_span("model_armor.sanitize_model_response") as span:
        span.set_attribute("model_armor.template_id", TEMPLATE_ID)
        span.set_attribute("model_armor.text_length", len(text))

        request = modelarmor_v1.SanitizeModelResponseRequest(
            name=_TEMPLATE_NAME,
            model_response_data=modelarmor_v1.DataItem(text=text),
        )

        try:
            response = _get_client().sanitize_model_response(request=request)
        except GoogleAPIError as exc:
            logger.exception("model_armor: sanitize_model_response call failed — blocking the write")
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, f"call failed: {exc}"))
            return ModelArmorScreenResult(
                blocked=True,
                reason=f"Model Armor call failed ({type(exc).__name__}) — blocked rather than writing unscreened",
                raw_filter_match_state="CALL_FAILED",
            )

        result = response.sanitization_result
        span.set_attribute("model_armor.invocation_result", result.invocation_result.name)
        span.set_attribute("model_armor.filter_match_state", result.filter_match_state.name)

        if result.invocation_result != modelarmor_v1.InvocationResult.SUCCESS:
            # PARTIAL means some filters were skipped server-side — a clean
            # NO_MATCH_FOUND from a partial run isn't a real clearance.
            reason = f"Model Armor filters did not fully execute ({result.invocation_result.name})"
            span.set_status(trace.Status(trace.StatusCode.ERROR, reason))
            return ModelArmorScreenResult(blocked=True, reason=reason, raw_filter_match_state=result.filter_match_state.name)

        if result.filter_match_state != modelarmor_v1.FilterMatchState.MATCH_FOUND:
            return ModelArmorScreenResult(blocked=False, raw_filter_match_state=result.filter_match_state.name)

        reason = _describe_match(result)
        span.set_attribute("model_armor.block_reason", reason)
        span.set_status(trace.Status(trace.StatusCode.ERROR, reason))
        return ModelArmorScreenResult(blocked=True, reason=reason, raw_filter_match_state=result.filter_match_state.name)


def _describe_match(result: modelarmor_v1.SanitizationResult) -> str:
    """Turns the per-filter result map into one human-readable reason,
    shown verbatim in the action_outcome node the HITL panel renders."""
    MATCH = modelarmor_v1.FilterMatchState.MATCH_FOUND
    reasons: list[str] = []

    rai = result.filter_results.get("rai")
    if rai is not None and rai.rai_filter_result.match_state == MATCH:
        hit = sorted(
            category
            for category, category_result in rai.rai_filter_result.rai_filter_type_results.items()
            if category_result.match_state == MATCH
        )
        reasons.append(f"responsible-AI filter ({', '.join(hit) or 'unspecified category'})")

    pi = result.filter_results.get("pi_and_jailbreak")
    if pi is not None and pi.pi_and_jailbreak_filter_result.match_state == MATCH:
        reasons.append("prompt injection / jailbreak pattern detected")

    uri = result.filter_results.get("malicious_uris")
    if uri is not None and uri.malicious_uri_filter_result.match_state == MATCH:
        reasons.append("malicious URL detected")

    sdp = result.filter_results.get("sdp")
    if sdp is not None and sdp.sdp_filter_result.inspect_result.match_state == MATCH:
        info_types = sorted({f.info_type for f in sdp.sdp_filter_result.inspect_result.findings})
        reasons.append(f"sensitive data detected ({', '.join(info_types) or 'unspecified type'})")

    return "; ".join(reasons) if reasons else "Model Armor flagged this content"
