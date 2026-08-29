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

import os
from functools import cached_property

from dotenv import load_dotenv
from google.adk.models import Gemini
from google.genai import Client as GenaiClient
from google.genai import types

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

    MedGemma's endpoint is a plain vLLM container, so its fallback cannot go
    through ADK's LlmAgent machinery — it needs the raw SDK. Sharing the
    client here keeps the `global` location and the retry policy identical to
    every agent's, rather than re-deriving them at a second call site.
    """
    return GenaiClient(
        vertexai=True,
        project=PROJECT_ID,
        location=GEMINI_LOCATION,
        http_options=types.HttpOptions(retry_options=_RETRY_OPTIONS),
    )
