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


async def generate_structured(
    step: str,
    instruction: str,
    payload: str,
    schema_model: type[BaseModel],
    max_tokens: int = 2048,
    attempts: int = 2,
) -> BaseModel:
    """Real MedGemma call whose output IS the step's result.

    ADK's LlmAgent+output_schema path can't be reused here: that is bound to
    a Gemini BaseLlm, and this endpoint is a plain Vertex vLLM
    chat-completions container. So the schema is appended to the prompt and
    the response is validated against the SAME Pydantic model the ADK path
    would have enforced — the guarantee the caller gets is unchanged, only
    the mechanism differs.

    Retries once on a parse/validation failure (a small model occasionally
    emits a stray prose preamble). After that it raises: there is deliberately
    no Gemini fallback, because a step that claims to be written by a
    medical-domain model must not quietly be written by a general one.
    """
    if not medgemma_available():
        raise MedGemmaError(
            f"medgemma[{step}]: MEDGEMMA_ENDPOINT_ID is not set. This step runs on MedGemma with "
            "no fallback, so it cannot proceed unconfigured."
        )

    schema_block = json.dumps(schema_model.model_json_schema(), indent=2)
    prompt = (
        f"{instruction}\n\n{payload}\n\n"
        "Respond with ONLY a JSON object matching this schema. No markdown fence, no prose.\n\n"
        f"{schema_block}"
    )

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
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
            return result
        except Exception as exc:  # noqa: BLE001 — retried, then re-raised below
            last_error = exc
            logger.warning(
                "medgemma[%s]: attempt %d/%d failed after %.2fs: %s",
                step,
                attempt,
                attempts,
                time.monotonic() - start,
                exc,
            )

    raise MedGemmaError(f"medgemma[{step}]: no valid {schema_model.__name__} after {attempts} attempts") from last_error
