"""Real-latency shadow comparison against a self-deployed MedGemma 4B
endpoint — Complication Reasoning and Documentation only, per direct request.

DELIBERATELY SHADOW-ONLY. MedGemma's response is logged, never fed into the
graph — the pipeline's real behavior (what gets written, what the
verification gate sees) is completely unchanged by this file. This is
observability, not a new decision path. Fired as a background asyncio task
(never awaited inline), so a slow or failed MedGemma call can never add
latency to, or break, the real pipeline it's only supposed to be watching.

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
import logging
import os
import time

from dotenv import load_dotenv
from google.cloud import aiplatform

load_dotenv()

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
REGION = os.environ.get("SURGGRAPH_REGION", "us-central1")
# Set once the endpoint is actually deployed — nothing here fires until it
# is, same convention as SurgBot's own MEDGEMMA_API_BASE-gated design.
MEDGEMMA_ENDPOINT_ID = os.environ.get("MEDGEMMA_ENDPOINT_ID")

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
