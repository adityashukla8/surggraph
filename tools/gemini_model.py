"""Shared Gemini model configuration for every ADK agent in this system.

Finding from Day-1 Spike B: gemini-3.5-flash (the hackathon's mandatory
model) 404s on every REGIONAL Vertex AI endpoint tested for this project
(us-central1, us-east5, us-east1, europe-west4) — gemini-2.5-flash works
regionally, but that's older than required. gemini-3.5-flash is only
reachable via the Vertex AI `global` location. This may just reflect this
model's current rollout stage (broader regional availability may follow) —
if a future agent build hits a 404 on GlobalGemini, re-check regional
availability before assuming this workaround is still needed.

ADK's Gemini model wrapper doesn't expose `location` as a constructor
field, so this uses the documented subclass-override pattern (see
google.adk.models.Gemini's docstring) to force the global endpoint.
Every agent should build its model via new_agent_model() rather than
passing a bare model name string to Agent(model=...).

Retry-with-backoff finding (Monitor Agent build): gemini-3.5-flash has no
dimensioned quota bucket anywhere on this project (confirmed via `gcloud
alpha services quota list` — only gemini-3.5-flash-cyber/-lite-qcd/
-transcribe-preview exist as named rows). It runs on Vertex AI's
pay-as-you-go Dynamic Shared Quota pool instead of a fixed per-project
number — 429 RESOURCE_EXHAUSTED under concurrent load is expected,
normal behavior there, not a symptom of a misconfigured/low quota to
request an increase for (a `quota update` override attempt confirmed
self-service increase is disabled for this dimension:
COMMON_QUOTA_CONSUMER_OVERRIDE_TOO_HIGH, max=0). Google's own guidance
(cloud.google.com/blog/products/ai-machine-learning/reduce-429-errors-on-vertex-ai)
is exponential backoff with jitter — configured here via `HttpRetryOptions`
so every agent gets it automatically, rather than each agent wrapping its
own calls individually.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from functools import cached_property

from dotenv import load_dotenv
from google.adk.models import Gemini
from google.genai import Client as GenaiClient
from google.genai import types
from pydantic import BaseModel

load_dotenv()

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_LOCATION = os.environ.get("GEMINI_LOCATION", "global")


# Google's documented defaults are attempts=5, initial_delay=1.0s, exp_base=2.0,
# jitter=1.0 — widened here (more attempts, longer max_delay) since Dynamic
# Shared Quota contention under this project's concurrent multi-agent workload
# (Monitor's 3 sub-agents per window) is expected to need more headroom than
# the SDK's single-request-oriented default.
_RETRY_OPTIONS = types.HttpRetryOptions(
    attempts=8,
    initial_delay=2.0,
    max_delay=90.0,
    exp_base=2.0,
    jitter=1.0,
    http_status_codes=[408, 429, 500, 502, 503, 504],
)


class GlobalGemini(Gemini):
    @cached_property
    def api_client(self) -> GenaiClient:
        return GenaiClient(
            vertexai=True,
            project=PROJECT_ID,
            location=GEMINI_LOCATION,
            http_options=types.HttpOptions(retry_options=_RETRY_OPTIONS),
        )


def new_agent_model(model_name: str = GEMINI_MODEL) -> GlobalGemini:
    return GlobalGemini(model=model_name)


def new_genai_client() -> GenaiClient:
    """The same configured client, for the paths that are not ADK agents.

    Documentation's generate_structured() calls raw google-genai rather than
    going through ADK's LlmAgent machinery, so it needs this directly. Sharing
    the client here keeps the `global` location and the retry policy identical
    to every agent's, rather than re-deriving them at a second call site.
    """
    return GenaiClient(
        vertexai=True,
        project=PROJECT_ID,
        location=GEMINI_LOCATION,
        http_options=types.HttpOptions(retry_options=_RETRY_OPTIONS),
    )


class StructuredGenerationError(RuntimeError):
    """Gemini could not produce a result matching the schema after retrying."""


def _strip_fence(text: str) -> str:
    """Real, observed behaviour: a fenced ```json block despite being told not
    to. Recovering from that is not tolerating malformed output — anything
    that still fails to parse after this raises."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
        if text.lower().startswith("json"):
            text = text[4:]
    return text.strip()


async def generate_structured(
    step: str,
    instruction: str,
    payload: str,
    schema_model: type[BaseModel],
    max_tokens: int = 2048,
    attempts: int = 2,
) -> BaseModel:
    """Gemini 3.5 call whose output IS the step's result, schema-validated.

    Was MedGemma with a Gemini fallback (docs/qa_log.md has the history —
    the medical-domain model was undeployed after repeated generation
    failures on certain cases: truncated JSON regardless of machine shape,
    GPU count, or token ceiling). Gemini is now the only model for this step,
    used with response_schema for native schema enforcement rather than the
    prompt-appended-schema + manual retry MedGemma needed.
    """
    client = new_genai_client()
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
            response = await asyncio.to_thread(
                lambda: client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=schema_model,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        max_output_tokens=max_tokens,
                        temperature=0.2,
                    ),
                )
            )
            elapsed = time.monotonic() - start
            result = schema_model.model_validate_json(_strip_fence(response.text))
            logging.getLogger(__name__).info(
                "generate_structured[%s]: %.2fs, valid %s (attempt %d)",
                step, elapsed, schema_model.__name__, attempt,
            )
            return result
        except Exception as exc:  # noqa: BLE001 — retried, then re-raised below
            last_error = exc
            logging.getLogger(__name__).warning(
                "generate_structured[%s]: attempt %d/%d failed after %.2fs: %s",
                step, attempt, attempts, time.monotonic() - start, exc,
            )

    raise StructuredGenerationError(
        f"generate_structured[{step}]: no valid {schema_model.__name__} after {attempts} attempts"
    ) from last_error
