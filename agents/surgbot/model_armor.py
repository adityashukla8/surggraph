"""SurgBot's own Model Armor screening — a NEW file, deliberately not an edit
to tools/model_armor.py (which is hardcoded to the surggraph-fhir-outbound
template and the documentation agent's section-joining helper, and is on the
hard "never touch" allowlist for this build).

Same ModelArmorClient usage pattern as tools/model_armor.py, own independent
client, own new template: `surggraph-surgbot-review-outbound`, provisioned
for real via `gcloud model-armor templates create` (an operator action, not
app code — verified this session: identical filter shape to the existing
surggraph-fhir-outbound template — RAI filters at MEDIUM_AND_ABOVE, PI/
jailbreak detection at HIGH confidence, malicious URL detection, SDP Basic
mode), the exact command run and confirmed via `gcloud model-armor templates
describe surggraph-surgbot-review-outbound --location=us-central1`.

Two real enforcement points on the SAME template — a template is a filter
policy, not a direction:

1. screen_review_document (sanitize_model_response) — screens the draft
   review document's synthesized narrative before it's ever shown to a
   surgeon as an approvable artifact (Phase 5, root_agent.py's
   draft_review_document tool). Gemini-generated content about a real
   case, synthesized from real case-graph text (including literature
   abstracts pulled live from third-party sources) — the same "model-
   generated content about to leave this reasoning loop" risk tools/
   model_armor.py already screens for on the operative-record path.

2. screen_user_input (sanitize_user_prompt, added 2026-08-28) — screens a
   reviewer's raw turn (voice transcript or typed text) BEFORE it ever
   reaches the root agent's Gemini call. This closes a real gap found in
   a security review that day: this was the one untrusted-input surface
   in the whole codebase with no pre-LLM content-safety screen at all —
   every other Model Armor use in this project screened outbound
   (model-generated) content, never inbound (user-generated) content.

FAILS CLOSED, same contract as tools/model_armor.py: any GoogleAPIError
blocks rather than silently skipping the check, on both paths.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import GoogleAPIError
from google.cloud import modelarmor_v1
from pydantic import BaseModel

load_dotenv()

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
LOCATION = os.environ.get("SURGGRAPH_REGION", "us-central1")
TEMPLATE_ID = os.environ.get("SURGBOT_MODEL_ARMOR_TEMPLATE_ID", "surggraph-surgbot-review-outbound")

_TEMPLATE_NAME = f"projects/{PROJECT_ID}/locations/{LOCATION}/templates/{TEMPLATE_ID}"

_client: modelarmor_v1.ModelArmorClient | None = None


def _get_client() -> modelarmor_v1.ModelArmorClient:
    # Own lazy singleton, independent of tools/model_armor.py's — a shared
    # client instance would be harmless, but importing from a file on the
    # "never touch" allowlist for anything beyond reading it is the thing
    # actually being avoided here.
    global _client
    if _client is None:
        # Model Armor has no global endpoint for sanitize/template calls —
        # same regional-endpoint requirement tools/model_armor.py already
        # found (a request to the default global endpoint 404s regardless of
        # IAM).
        _client = modelarmor_v1.ModelArmorClient(
            client_options=ClientOptions(api_endpoint=f"modelarmor.{LOCATION}.rep.googleapis.com")
        )
    return _client


class ModelArmorScreenResult(BaseModel):
    blocked: bool
    reason: str | None = None
    raw_filter_match_state: str


def screen_user_input(text: str) -> ModelArmorScreenResult:
    """Runs a reviewer's raw turn (voice transcript or typed text) through
    sanitize_user_prompt — the real Model Armor GEAP component, same
    template as screen_review_document below (a template is just a filter
    policy; sanitize_user_prompt/sanitize_model_response are the two real
    enforcement points the SAME policy can sit at). This is the one real
    untrusted-input surface in this codebase that previously reached Gemini
    with no pre-screening at all (2026-08-28 security review finding) —
    called from services/surgbot_service/main.py::_send_turn_to_agent,
    BEFORE the turn is ever sent to the root agent, so a blocked input
    never reaches the LLM at all. Fails closed, same contract as
    screen_review_document."""
    request = modelarmor_v1.SanitizeUserPromptRequest(
        name=_TEMPLATE_NAME,
        user_prompt_data=modelarmor_v1.DataItem(text=text),
    )

    try:
        response = _get_client().sanitize_user_prompt(request=request)
    except GoogleAPIError as exc:
        logger.exception("surgbot model_armor: sanitize_user_prompt call failed — blocking the turn")
        return ModelArmorScreenResult(
            blocked=True,
            reason=f"Model Armor call failed ({type(exc).__name__}) — blocked rather than forwarding unscreened",
            raw_filter_match_state="CALL_FAILED",
        )

    result = response.sanitization_result

    if result.invocation_result != modelarmor_v1.InvocationResult.SUCCESS:
        return ModelArmorScreenResult(
            blocked=True,
            reason=f"Model Armor filters did not fully execute ({result.invocation_result.name})",
            raw_filter_match_state=result.filter_match_state.name,
        )

    if result.filter_match_state != modelarmor_v1.FilterMatchState.MATCH_FOUND:
        return ModelArmorScreenResult(blocked=False, raw_filter_match_state=result.filter_match_state.name)

    return ModelArmorScreenResult(
        blocked=True,
        reason=_describe_match(result),
        raw_filter_match_state=result.filter_match_state.name,
    )


def screen_review_document(text: str) -> ModelArmorScreenResult:
    """Runs the review document's plain narrative text through
    sanitize_model_response. `text` should be the joined, readable sections
    of the draft CaseReviewDocument — case_summary + follow_up_items +
    agreements + disagreements + coaching_notes joined, not any binary
    payload."""
    request = modelarmor_v1.SanitizeModelResponseRequest(
        name=_TEMPLATE_NAME,
        model_response_data=modelarmor_v1.DataItem(text=text),
    )

    try:
        response = _get_client().sanitize_model_response(request=request)
    except GoogleAPIError as exc:
        logger.exception("surgbot model_armor: sanitize_model_response call failed — blocking the draft")
        return ModelArmorScreenResult(
            blocked=True,
            reason=f"Model Armor call failed ({type(exc).__name__}) — blocked rather than showing unscreened",
            raw_filter_match_state="CALL_FAILED",
        )

    result = response.sanitization_result

    if result.invocation_result != modelarmor_v1.InvocationResult.SUCCESS:
        # PARTIAL means some filters were skipped server-side — a clean
        # NO_MATCH_FOUND from a partial run isn't a real clearance.
        return ModelArmorScreenResult(
            blocked=True,
            reason=f"Model Armor filters did not fully execute ({result.invocation_result.name})",
            raw_filter_match_state=result.filter_match_state.name,
        )

    if result.filter_match_state != modelarmor_v1.FilterMatchState.MATCH_FOUND:
        return ModelArmorScreenResult(blocked=False, raw_filter_match_state=result.filter_match_state.name)

    return ModelArmorScreenResult(
        blocked=True,
        reason=_describe_match(result),
        raw_filter_match_state=result.filter_match_state.name,
    )


def _describe_match(result: modelarmor_v1.SanitizationResult) -> str:
    """Same per-filter description logic as tools/model_armor.py::
    _describe_match — duplicated rather than imported, since importing from
    a file on the "never touch" allowlist for edits should not extend to a
    private helper this module has no business depending on staying stable."""
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
