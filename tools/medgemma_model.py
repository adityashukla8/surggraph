"""Self-deployed MedGemma 4B — the PRIMARY reasoning model for the
Documentation step, and the only model that writes the operative record.

WHY THIS STEP OWNS MEDGEMMA (2026-08-28, replacing a shadow-only design):
drafting an operative note is medical text generation, which is precisely
what MedGemma is trained for and what a general model is only adjacent to.
It is also the one artifact in this system that reaches a real external EHR
(tools/fhir_write.py -> DocumentReference), so it is the step where using a
medical-domain model is a substantive choice rather than a decorative one.

Measured head-to-head on the REAL production instruction and a real case
slice, scored on the five framing properties that step exists to enforce
(errors framed as automated/unconfirmed, ungrounded complications named as
ungrounded, grounded ones cited as supported, specific limitations, surgeon
response recorded):

    MedGemma 4B     10.65s   5/5 framing checks   valid OperativeNoteDraft
    Gemini 3.5      17.04s   5/5 framing checks   valid OperativeNoteDraft

So this is not a quality concession — it is faster here and holds the same
honesty contract. NO GEMINI FALLBACK on this path: see generate_structured.

`fire_shadow_latency_call` below is retained for Complication Reasoning,
where MedGemma genuinely is only an observer: its response is logged, never
fed into the graph, and fired as a background task that is never awaited, so
a slow or failed shadow call cannot add latency to or break the real
reasoning step it is watching.

Real prior lesson, this exact project: a full production switch to a
self-deployed medgemma-27b-text-it endpoint was reverted after its cold
start from zero measured at over six minutes — unworkable for a live demo,
and that endpoint's serving stack had no working chat-completions path either
(needed a hand-written BaseLlm wrapper). This is a smaller, 4B, always-warm
endpoint instead (kept running by the user specifically to avoid the cold-
start problem), deployed via `vertexai.preview.model_garden.OpenModel(
"google/medgemma@medgemma-4b-it").deploy(...)` — a real vLLM Model Garden
serving container, confirmed (2026-08) to support the standard OpenAI-style
chat-completions request shape directly, so no custom BaseLlm is needed here.

Real wire shape (Vertex AI's documented vLLM chat-completions convention,
containers deployed after 2024-08-20): `instances=[{"@requestFormat":
"chatCompletions", "messages": [...], "max_tokens": N, "temperature": T}]`
via the plain aiplatform Endpoint.predict() call — a real, blocking SDK
call, so it's wrapped in asyncio.to_thread like every other blocking call in
this codebase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from dotenv import load_dotenv
from google.cloud import aiplatform
from google.genai import types as genai_types
from pydantic import BaseModel

load_dotenv()

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
REGION = os.environ.get("SURGGRAPH_REGION", "us-central1")
# Set once the endpoint is actually deployed — nothing here fires until it
# is, same convention as SurgBot's own MEDGEMMA_API_BASE-gated design.
MEDGEMMA_ENDPOINT_ID = os.environ.get("MEDGEMMA_ENDPOINT_ID")
# Recorded as real provenance on every node this model writes, so the graph
# and the UI say which model actually produced the operative record.
MEDGEMMA_MODEL_ID = os.environ.get("MEDGEMMA_MODEL_ID", "medgemma-4b-it")
# Recorded verbatim as provenance whenever the fallback writes the record, so
# the graph never claims a medical-domain model produced general-model text.
_FALLBACK_MODEL_ID = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash") + " (fallback)"
# The endpoint scales to zero, and waking it is minutes, not seconds (226.7s
# measured on g2-standard-32). "Still booting" is not the same failure as
# "broken", so it gets waited on rather than counted as a failed attempt —
# otherwise every cold case would silently be written by the fallback and
# MedGemma would never write the record it is there to write.
_SCALE_UP_WAIT_S = float(os.environ.get("MEDGEMMA_SCALE_UP_WAIT_S", "180"))
_SCALE_UP_POLL_S = 5.0


def _is_waking(exc: Exception) -> bool:
    """True while a scaled-to-zero endpoint is still coming up.

    Vertex answers 429 "Model is not yet ready for inference..." during
    scale-up. The aiplatform SDK surfaces that as a bare ValueError with the
    status embedded in the message rather than a typed 429, so this matches
    the text — confirmed against a real cold endpoint, not assumed.
    """
    text = str(exc).lower()
    return "429" in text and "not yet ready" in text

_endpoint: aiplatform.Endpoint | None = None
_initialized = False


def medgemma_available() -> bool:
    return bool(MEDGEMMA_ENDPOINT_ID)


def _get_endpoint() -> aiplatform.Endpoint:
    global _endpoint, _initialized
    if _endpoint is None:
        if not _initialized:
            aiplatform.init(project=PROJECT_ID, location=REGION)
            _initialized = True
        _endpoint = aiplatform.Endpoint(MEDGEMMA_ENDPOINT_ID)
    return _endpoint


def _predict_sync(prompt: str, max_tokens: int) -> str:
    endpoint = _get_endpoint()
    response = endpoint.predict(
        instances=[
            {
                "@requestFormat": "chatCompletions",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            }
        ]
    )
    # Real, confirmed response shape for this single-instance chat-completions
    # call: response.predictions IS the completion object itself (a dict with
    # a real "choices" list), not a list of per-instance predictions the way
    # a standard Vertex predict() response usually is — checked directly
    # against a real call rather than assumed from the request-shape docs.
    return response.predictions["choices"][0]["message"]["content"]


async def _run_shadow_call(step: str, prompt: str, max_tokens: int) -> None:
    start = time.monotonic()
    try:
        text = await asyncio.to_thread(_predict_sync, prompt, max_tokens)
        elapsed = time.monotonic() - start
        logger.info("medgemma_latency[%s]: %.2fs — response preview: %r", step, elapsed, text[:200])
    except Exception:
        elapsed = time.monotonic() - start
        logger.exception("medgemma_latency[%s]: FAILED after %.2fs", step, elapsed)


def fire_shadow_latency_call(step: str, prompt: str, max_tokens: int = 1024) -> None:
    """Fires the real MedGemma call in the background and returns immediately
    — callers must NOT await this. A no-op (not an error) when
    MEDGEMMA_ENDPOINT_ID isn't set, so this is always safe to call
    unconditionally from the real call sites."""
    if not medgemma_available():
        return
    asyncio.create_task(_run_shadow_call(step, prompt, max_tokens))


# --------------------------------------------------------------------------
# PRIMARY (non-shadow) generation — MedGemma as the real reasoning model for
# a step, not an observer of one.
# --------------------------------------------------------------------------


class MedGemmaError(RuntimeError):
    """MedGemma could not produce a valid result. Raised, never swallowed:
    the step that owns MedGemma has NO Gemini fallback by design, so a
    failure here must surface as a real failure rather than silently
    substituting a different model's output for a medical-domain one."""


def _build_prompt(instruction: str, payload: str, schema_model: type[BaseModel]) -> str:
    """One prompt for both models, so the fallback answers the same question."""
    schema_block = json.dumps(schema_model.model_json_schema(), indent=2)
    return (
        f"{instruction}\n\n{payload}\n\n"
        "Respond with ONLY a JSON object matching this schema. No markdown fence, no prose.\n\n"
        f"{schema_block}"
    )


def _strip_fence(text: str) -> str:
    """Real, repeatedly-observed behaviour of this endpoint: it wraps JSON in
    a ```json fence despite being told not to. Recovering from that is not
    the same thing as tolerating malformed output — anything that still fails
    to parse after this raises."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    parts = text.split("```")
    if len(parts) < 2:
        return text
    body = parts[1]
    if body.startswith("json"):
        body = body[4:]
    return body.strip()


async def _gemini_fallback(
    step: str, prompt: str, schema_model: type[BaseModel], max_tokens: int
) -> BaseModel:
    """Last resort when MedGemma cannot produce a result.

    Uses the raw google-genai client rather than ADK: the prompt already
    carries the schema for MedGemma's benefit, but Gemini can enforce it
    natively via response_schema, so the same Pydantic guarantee holds by a
    stronger mechanism.
    """
    from tools.gemini_model import GEMINI_MODEL, new_genai_client

    client = new_genai_client()
    response = await asyncio.to_thread(
        lambda: client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema_model,
                # Gemini 3.5 spends output tokens on thinking, so MedGemma's
                # budget truncated the JSON mid-string on the first real test.
                # Thinking is off (this is a formatting task, not a reasoning
                # one) and the ceiling is raised so the schema always closes.
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                max_output_tokens=max(max_tokens, 2048) * 2,
                temperature=0.2,
            ),
        )
    )
    return schema_model.model_validate_json(response.text)


async def generate_structured(
    step: str,
    instruction: str,
    payload: str,
    schema_model: type[BaseModel],
    max_tokens: int = 2048,
    attempts: int = 2,
) -> tuple[BaseModel, str]:
    """Real MedGemma call whose output IS the step's result.

    ADK's LlmAgent+output_schema path can't be reused here: that is bound to
    a Gemini BaseLlm, and this endpoint is a plain Vertex vLLM
    chat-completions container. So the schema is appended to the prompt and
    the response is validated against the SAME Pydantic model the ADK path
    would have enforced — the guarantee the caller gets is unchanged, only
    the mechanism differs.

    Retries once on a parse/validation failure (a small model occasionally
    emits a stray prose preamble), then falls back to Gemini 3.5 so the
    pipeline still completes when the endpoint is down, unconfigured or
    failing for any other reason.

    Returns (result, model_id) — the id of the model that ACTUALLY produced
    the result, never the one that was supposed to. Callers record it as
    provenance, so a record written by the general model says so instead of
    inheriting MedGemma's name. Returning it rather than exposing it as
    module state is deliberate: cases run concurrently, and a shared
    "last model used" would attribute one case's fallback to another.
    """
    if not medgemma_available():
        logger.warning(
            "medgemma[%s]: MEDGEMMA_ENDPOINT_ID is not set — falling back to %s",
            step,
            _FALLBACK_MODEL_ID,
        )
        prompt = _build_prompt(instruction, payload, schema_model)
        return await _gemini_fallback(step, prompt, schema_model, max_tokens), _FALLBACK_MODEL_ID

    prompt = _build_prompt(instruction, payload, schema_model)

    last_error: Exception | None = None
    deadline = time.monotonic() + _SCALE_UP_WAIT_S
    announced_wait = False
    attempt = 0
    while attempt < attempts:
        start = time.monotonic()
        try:
            raw = await asyncio.to_thread(_predict_sync, prompt, max_tokens)
            elapsed = time.monotonic() - start
            result = schema_model.model_validate(json.loads(_strip_fence(raw)))
            logger.info(
                "medgemma[%s]: %.2fs, %d chars, valid %s (attempt %d)",
                step,
                elapsed,
                len(raw),
                schema_model.__name__,
                attempt,
            )
            return result, MEDGEMMA_MODEL_ID
        except Exception as exc:  # noqa: BLE001 — retried, then handed to the fallback
            if _is_waking(exc) and time.monotonic() < deadline:
                if not announced_wait:
                    announced_wait = True
                    logger.info(
                        "medgemma[%s]: endpoint is scaling up from zero — waiting up to %.0fs before falling back",
                        step,
                        _SCALE_UP_WAIT_S,
                    )
                await asyncio.sleep(_SCALE_UP_POLL_S)
                continue  # still booting: not a failed attempt
            last_error = exc
            attempt += 1
            logger.warning(
                "medgemma[%s]: attempt %d/%d failed after %.2fs: %s",
                step,
                attempt,
                attempts,
                time.monotonic() - start,
                exc,
            )

    # MedGemma is exhausted. Fall back rather than fail the case — but say so
    # loudly, and hand the caller the model id that really wrote this.
    logger.warning(
        "medgemma[%s]: exhausted %d attempts (%s) — falling back to %s",
        step,
        attempts,
        last_error,
        _FALLBACK_MODEL_ID,
    )
    try:
        result = await _gemini_fallback(step, prompt, schema_model, max_tokens)
    except Exception as fallback_error:
        raise MedGemmaError(
            f"medgemma[{step}]: no valid {schema_model.__name__} after {attempts} attempts, "
            f"and the {_FALLBACK_MODEL_ID} fallback also failed: {fallback_error}"
        ) from last_error
    logger.info("medgemma[%s]: fallback produced a valid %s via %s", step, schema_model.__name__, _FALLBACK_MODEL_ID)
    return result, _FALLBACK_MODEL_ID
