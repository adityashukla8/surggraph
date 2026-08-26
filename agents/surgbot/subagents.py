"""TEXT reasoning subagents — Gemini 3.5 via tools/gemini_model.py::
new_agent_model(), tools=[], real output_schema. Same convention every other
agent in this codebase follows (verified: agents/divergence_detection/
subagent.py's build_subagent() is the template this mirrors).

ARCHITECTURE DECISION (this session, supersedes the original in-process
design): every one of these subagents is its OWN separately-deployed Agent
Runtime reasoning engine, deployed via the STABLE vertexai.agent_engines.
AdkApp WITH identity_type=AGENT_IDENTITY — confirmed working end to end this
session via a real deploy+invoke of a plain Gemini-3.5 tool-calling agent
with that identity type set. (root_agent.py used to run on a separate Live
API model that couldn't use AGENT_IDENTITY at all — see plan_v2 §15, which
migrated root_agent.py off the Live API entirely; it now deploys via this
SAME STABLE AdkApp class, just kept on service_account rather than
identity_type for this pass — see scripts/deploy_surgbot_agent.py.)
root_agent.py's tools invoke these REMOTELY via async_stream_query — the
exact pattern already proven in scripts/spike_deploy_stub_agent.py::invoke()
— never by constructing a local in-process LlmAgent. "Everything GEAP
deployed" is a literal architecture requirement here, not just a
Runtime-for-the-root-agent claim.

Deployed resource names are cached in .deployed_subagents.json (gitignored,
next to this file) so re-running a deploy script doesn't redeploy on every
invocation. deploy_or_get_subagent() checks, in order: (1) a
SURGBOT_SUBAGENT_RESOURCE_<KIND> env var — how the DEPLOYED root agent
finds these in production, baked in at root-agent deploy time by
scripts/deploy_surgbot_agent.py, since the deployed sandbox never has this
module's local cache file; (2) the cache file, for local runs of
scripts/deploy_surgbot_subagents.py; (3) a live client.agent_engines.list()
scan by display_name, the last-resort fallback; only deploying fresh if
none of the three finds a live match.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Literal

import vertexai
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field
from vertexai import agent_engines
from vertexai import types as vertexai_types

from tools.gemini_model import GEMINI_MODEL, new_agent_model

load_dotenv()

logger = logging.getLogger(__name__)

# LAZY, NOT MODULE-LEVEL — real bug found via Cloud Logging this session
# (aiplatform.googleapis.com/reasoning_engine_stderr on a failed subagent
# deploy): this module's own output-schema classes (ErrorChainReview etc.)
# get cloudpickled by reference, so the DEPLOYED sandbox re-imports this
# exact module too — but a deployed subagent sandbox only ever gets the env
# vars scripts/deploy_surgbot_subagents.py explicitly passes it (project/
# model/location/telemetry), never SURGGRAPH_GCS_BUCKET, since a running
# subagent has no reason to itself deploy anything. A module-level
# `os.environ["SURGGRAPH_GCS_BUCKET"]` therefore raised KeyError on import
# inside every deployed sandbox, before the agent could ever serve traffic.
# Deploy-only values are computed inside the functions that actually deploy,
# never at import time.
SubagentKind = Literal["error_chain_reviewer", "synthesis", "pattern_insight"]
SUBAGENT_KINDS: tuple[SubagentKind, ...] = ("error_chain_reviewer", "synthesis", "pattern_insight")

_DEPLOY_CACHE_PATH = Path(__file__).resolve().parent / ".deployed_subagents.json"

# Real, documented telemetry switches (tools/observability.py, agents/surgbot/
# subagents deploy scripts) — set on EVERY SurgBot deployment, subagents
# included, so every real reasoning component ships genuine OTel GenAI spans
# to Cloud Trace, not just the root agent.
_TELEMETRY_ENV_VARS = {
    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
    "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
}


# --- Output schemas -----------------------------------------------------------


class ErrorChainReview(BaseModel):
    """Phase 3's structured output: what happened, whether it's plausible
    given the evidence, and what the literature actually says — fed from
    slices.error_chain_slice's output (the error, its linked complications,
    and any literature evidence already on the graph)."""

    mechanism_summary: str  # plain-language account of error -> complication
    plausibility_probe: str  # is this causal chain actually well-supported, or a stretch?
    citation_summary: str  # what the attached literature evidence actually says, or "no literature attached"
    confidence: float = Field(ge=0, le=1)


class SynthesisDraft(BaseModel):
    """Phase 5's structured output — the case-review draft. Maps directly
    onto agents/surgbot/schema.py::CaseReviewDocument's own fields, so
    root_agent.py's draft_review_document tool can build the document by
    unpacking this response with no extra mapping layer."""

    case_summary: str
    follow_up_items: list[str] = Field(default_factory=list)
    agreements: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    coaching_notes: list[str] = Field(default_factory=list)
    threshold_adjustments: list[str] = Field(default_factory=list)


class PatternInsight(BaseModel):
    """Phase 6's structured output — cross-session pattern framing, fed real
    retrieve_memories() results. has_history is required precisely so a
    reviewer's first-ever session renders an honest "no prior history yet"
    rather than a confident-sounding but fabricated pattern claim."""

    has_history: bool
    pattern_summary: str  # "no prior session history yet" when has_history is False
    supporting_memory_count: int = 0
    caveats: str = ""


_OUTPUT_SCHEMAS: dict[SubagentKind, type[BaseModel]] = {
    "error_chain_reviewer": ErrorChainReview,
    "synthesis": SynthesisDraft,
    "pattern_insight": PatternInsight,
}

_INSTRUCTIONS: dict[SubagentKind, str] = {
    "error_chain_reviewer": """You are the Error Chain Reviewer subagent for SurgBot, a post-hoc
surgical case review assistant. You are given one JSON slice: a detected
error, the complication(s) it was causally linked to during the case, and any
literature evidence already attached to those complications.

Produce:
- mechanism_summary: a plain-language account of how this error is claimed to
  have led to this complication, written for a surgeon skimming a review, not
  a re-statement of the raw graph attrs.
- plausibility_probe: a genuinely critical assessment of whether this causal
  chain is well-supported by what's actually in the slice, or a stretch — say
  so plainly if the evidence is thin. Do not soften a weak chain to sound more
  confident than it is.
- citation_summary: summarize what the attached literature evidence actually
  says about this mechanism. If literature_by_complication is empty for this
  complication, say "no literature attached" — never invent a citation.
- confidence: your own confidence in the mechanism_summary, 0 to 1.

Never invent facts not present in the slice you were given.""",
    "synthesis": """You are the Synthesis subagent for SurgBot, a post-hoc surgical case
review assistant. You are given the full record of one review session: the
case framing, every phase walked through, every error/complication chain
discussed, every corrective proposal and divergence alert discussed, and the
structured feedback (agree/disagree/uncertain verdicts, rationales, coaching
notes) the reviewing surgeon gave along the way.

Produce a case-review draft:
- case_summary: a concise narrative summary of the case and how the review
  session went.
- follow_up_items: concrete, actionable follow-ups (each a short, on it own).
- agreements: points where the reviewer explicitly agreed with the system's
  detections/proposals.
- disagreements: points where the reviewer explicitly disagreed, stated
  plainly (do not soften a real disagreement into a vague "some concerns").
- coaching_notes: coaching/teaching points the reviewer raised about the
  surgical technique or decision-making shown in the case.
- threshold_adjustments: any explicit suggestions the reviewer made about
  tuning detection thresholds or system behavior going forward.

Base every field ONLY on the session record you were given. If a category
has nothing real to report (e.g. no disagreements were raised), return an
empty list for it rather than inventing content to fill it.""",
    "pattern_insight": """You are the Pattern Insight subagent for SurgBot, a post-hoc surgical
case review assistant. You are given a reviewer's real, retrieved Memory Bank
facts from past SurgBot review sessions (may be empty).

If the memory list is empty, set has_history=false and pattern_summary to
"no prior session history yet for this reviewer" — this is a real, honest,
and completely normal result for a reviewer's first session, not a failure.

If the memory list is non-empty, set has_history=true and describe any real
recurring pattern you can actually support from the given facts (e.g. a
coaching theme that has come up more than once, a threshold adjustment
requested more than once). supporting_memory_count is how many of the given
memories actually support the pattern you describe — never a number larger
than the memories you were actually given. If the memories don't show any
real recurring pattern, say so in pattern_summary rather than manufacturing
one from a single data point.""",
}


def build_subagent(kind: SubagentKind) -> LlmAgent:
    """The local ADK Agent definition for one subagent kind — this is what
    gets wrapped in AdkApp and deployed; it is never run in-process by
    root_agent.py directly (see module docstring)."""
    return LlmAgent(
        name=f"surgbot_{kind}",
        model=new_agent_model(),
        instruction=_INSTRUCTIONS[kind],
        output_schema=_OUTPUT_SCHEMAS[kind],
    )


def _display_name(kind: SubagentKind) -> str:
    return f"surgbot-{kind}"


def _load_cache() -> dict[str, str]:
    if not _DEPLOY_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_DEPLOY_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("subagents: .deployed_subagents.json unreadable, ignoring cache", exc_info=True)
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    _DEPLOY_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _get_client() -> vertexai.Client:
    project_id = os.environ["SURGGRAPH_PROJECT_ID"]
    region = os.environ.get("SURGGRAPH_REGION", "us-central1")
    return vertexai.Client(project=project_id, location=region)


def _find_existing_by_display_name(client: vertexai.Client, display_name: str):
    for engine in client.agent_engines.list():
        if engine.api_resource.display_name == display_name:
            return engine
    return None


def deploy_or_get_subagent(kind: SubagentKind, client: vertexai.Client | None = None):
    """Returns a deployed Agent Runtime resource for `kind`. Real bug found
    via Cloud Logging this session: the deployed ROOT agent runs in its OWN
    isolated Agent Runtime sandbox, a different filesystem than the local
    dev machine that ran scripts/deploy_surgbot_subagents.py — so
    .deployed_subagents.json (this module's on-disk cache, written by that
    script) is never actually present there, meaning every single tool call
    that reaches this function from inside the deployed root agent was
    silently falling through to a live client.agent_engines.list() scan
    every time (correct, but a real, avoidable round trip on every call).

    Fix, mirroring the STATE_SERVICE_URL fix in scripts/deploy_surgbot_agent.
    py: scripts/deploy_surgbot_agent.py now bakes each subagent's real
    resource name into the ROOT agent's own deploy-time env vars
    (SURGBOT_SUBAGENT_RESOURCE_<KIND>), so the common path here is a single
    cheap client.agent_engines.get() by exact name — checked first, before
    the cache file (which still matters for deploy_surgbot_subagents.py's
    own local re-runs) and the live list() scan (kept as the last-resort
    fallback for local/dev runs with no env var set, or a stale env value)."""
    client = client or _get_client()
    display_name = _display_name(kind)

    env_key = f"SURGBOT_SUBAGENT_RESOURCE_{kind.upper()}"
    env_resource = os.environ.get(env_key)
    if env_resource:
        try:
            existing = client.agent_engines.get(name=env_resource)
            logger.info("subagents: using env-pinned deployment for %s: %s", kind, env_resource)
            return existing
        except Exception:
            logger.warning(
                "subagents: env-pinned resource for %s (%s=%s) no longer resolves, falling back",
                kind, env_key, env_resource,
            )

    cache = _load_cache()
    cached_name = cache.get(kind)
    if cached_name:
        try:
            existing = client.agent_engines.get(name=cached_name)
            logger.info("subagents: reusing cached deployment for %s: %s", kind, cached_name)
            return existing
        except Exception:
            logger.warning("subagents: cached resource for %s (%s) no longer resolves, re-checking", kind, cached_name)

    found = _find_existing_by_display_name(client, display_name)
    if found is not None:
        cache[kind] = found.api_resource.name
        _save_cache(cache)
        logger.info("subagents: found existing live deployment for %s: %s", kind, found.api_resource.name)
        return found

    logger.info("subagents: no existing deployment found for %s — deploying fresh", kind)
    agent = build_subagent(kind)
    app = agent_engines.AdkApp(agent=agent)  # STABLE class — no bidi needed for a plain text subagent

    project_id = os.environ["SURGGRAPH_PROJECT_ID"]
    staging_bucket = f"gs://{os.environ['SURGGRAPH_GCS_BUCKET']}"

    engine = client.agent_engines.create(
        agent=app,
        config={
            "display_name": display_name,
            "requirements": [
                "google-cloud-aiplatform[agent_engines,adk]",
                "pydantic",
                "cloudpickle",
                "python-dotenv",
            ],
            "extra_packages": ["agents", "tools", "state"],
            "staging_bucket": staging_bucket,
            "env_vars": {
                "SURGGRAPH_PROJECT_ID": project_id,
                "GEMINI_MODEL": GEMINI_MODEL,
                "GEMINI_LOCATION": os.environ.get("GEMINI_LOCATION", "global"),
                **_TELEMETRY_ENV_VARS,
            },
            "identity_type": vertexai_types.IdentityType.AGENT_IDENTITY,
        },
    )
    cache[kind] = engine.api_resource.name
    _save_cache(cache)
    logger.info("subagents: deployed %s -> %s", kind, engine.api_resource.name)
    return engine


async def invoke_subagent(kind: SubagentKind, message: str, user_id: str = "surgbot-root", client: vertexai.Client | None = None) -> dict:
    """Invokes a deployed subagent remotely via async_stream_query — the
    exact pattern proven in scripts/spike_deploy_stub_agent.py::invoke() —
    and parses its final structured JSON response into the matching output
    schema. Returns a plain dict: {"parsed": <schema instance dict or None>,
    "raw_text": <the model's final text>, "error": <str or None>} so a
    caller can tell a real structured result apart from a fallback text blob
    without a raised exception the caller has to guess the shape of.

    deploy_or_get_subagent() is a plain synchronous function that can make a
    real blocking network call (client.agent_engines.get/list, or a full
    .create() if nothing is found) — real bug found this session via Cloud
    Logging: calling it directly from this coroutine freezes the WHOLE
    event loop for however long that call takes, during which the Live API
    connection can send no keepalive/audio at all. Run it in a thread.
    """
    client = client or _get_client()
    engine = await asyncio.to_thread(deploy_or_get_subagent, kind, client)

    session = await engine.async_create_session(user_id=user_id)
    session_id = session.get("id") if isinstance(session, dict) else session.id

    final_text = None
    async for event in engine.async_stream_query(user_id=user_id, session_id=session_id, message=message):
        content = event.get("content") if isinstance(event, dict) else None
        if not content:
            continue
        for part in content.get("parts", []):
            text = part.get("text")
            if text:
                final_text = text

    if final_text is None:
        return {"parsed": None, "raw_text": None, "error": f"{kind}: no text response from deployed subagent"}

    schema_cls = _OUTPUT_SCHEMAS[kind]
    try:
        parsed = schema_cls.model_validate_json(final_text)
        return {"parsed": parsed.model_dump(mode="json"), "raw_text": final_text, "error": None}
    except Exception as exc:
        logger.warning("subagents: %s response did not parse as %s: %s", kind, schema_cls.__name__, exc)
        return {"parsed": None, "raw_text": final_text, "error": f"response did not match {schema_cls.__name__}: {exc}"}
