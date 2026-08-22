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

import json
import logging
import os
import re
from functools import cached_property
from typing import AsyncGenerator

import aiohttp
import google.auth
import google.auth.credentials
import google.auth.transport.requests
from dotenv import load_dotenv
from google.adk.models import Gemini
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import Client as GenaiClient
from google.genai import types
from pydantic import BaseModel, PrivateAttr

logger = logging.getLogger(__name__)

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


# MedGemma has no serverless/global endpoint like Gemini above — it only runs
# on a self-deployed, GPU-backed Vertex AI Endpoint (Model Garden), and that
# endpoint only speaks Vertex's generic `:predict` RPC, not the `:generateContent`
# RPC ADK's native Gemini path (and LiteLLM's vertex_ai/gemma provider) assume.
# Confirmed live, not assumed:
#   - model="projects/.../endpoints/..." routes through ADK's native Gemini
#     client, which calls :generateContent -> real 404 (endpoint has no such
#     RPC).
#   - LiteLlm(model="vertex_ai/gemma/...") constructs the right kind of
#     :predict body in principle, but its own async transport cannot connect
#     to this endpoint's envoy front-end in this environment — reproduced
#     directly: plain aiohttp/requests reach the identical URL fine, LiteLLM's
#     internal aiohttp session does not.
# The endpoint's vLLM server (task label vllm-128k-context) does accept an
# OpenAI-chat-style request, but only when the instance body carries a
# Model-Garden-specific marker, "@requestFormat": "chatCompletions" — a bare
# {"messages": [...]} body 400s with "KeyError: missing required field
# 'prompt'"; adding the marker returns a real {"choices": [{"message": ...}]}.
# So this talks to the endpoint directly, bypassing both ADK's native path and
# LiteLLM, via ADK's own documented extension point for exactly this
# situation: LlmAgent.model accepts any BaseLlm instance, not just a name.
MEDGEMMA_MODEL_NAME = os.environ.get("MEDGEMMA_MODEL", "medgemma-27b-text-it")
MEDGEMMA_API_BASE = os.environ.get("MEDGEMMA_API_BASE")

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class MedGemmaEndpointModel(BaseLlm):
    """Calls a self-deployed MedGemma Vertex AI Endpoint's :predict RPC using
    the Model Garden vLLM chat-completions wire format directly.

    Not a general-purpose Vertex Endpoint client — the request shape here (the
    "@requestFormat": "chatCompletions" marker, the response parsing) is
    specific to what THIS deployment was empirically confirmed to accept; a
    different Model Garden serving container could need a different shape.
    """

    api_base: str
    max_output_tokens: int = 2048

    _credentials: google.auth.credentials.Credentials | None = PrivateAttr(default=None)

    def _access_token(self) -> str:
        # Same ADC mechanism GlobalGemini relies on above, refreshed lazily
        # (google.auth.Credentials tracks its own expiry — .refresh() only
        # hits the network when actually needed, not on every call).
        if self._credentials is None:
            self._credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        if not self._credentials.valid:
            self._credentials.refresh(google.auth.transport.requests.Request())
        return self._credentials.token

    @staticmethod
    def _content_to_message(content: types.Content) -> dict:
        role = "assistant" if content.role == "model" else "user"
        text = "".join(part.text or "" for part in (content.parts or []))
        return {"role": role, "content": text}

    @staticmethod
    def _strip_schema_prose(node):
        # pydantic's model_json_schema() embeds each field's/class's docstring
        # verbatim as "description" (and a "title" derived from the field
        # name) — real prose, not structural shape. Confirmed live: with this
        # left in, MedGemma echoed the schema's own long "description" back as
        # literal content in its answer, alongside the real fields, because
        # nothing here enforces schema-constrained decoding the way Gemini's
        # native output_schema does — the model is just reading instructions
        # in the prompt and can misread a schema wrapper as part of the
        # content to reproduce. Stripping title/description recursively keeps
        # only what actually constrains the shape (type/properties/required).
        if isinstance(node, dict):
            return {
                k: MedGemmaEndpointModel._strip_schema_prose(v)
                for k, v in node.items()
                if k not in ("title", "description")
            }
        if isinstance(node, list):
            return [MedGemmaEndpointModel._strip_schema_prose(v) for v in node]
        return node

    def _build_messages(self, llm_request: LlmRequest) -> list[dict]:
        system_instruction = llm_request.config.system_instruction
        response_schema = llm_request.config.response_schema
        system_text = system_instruction if isinstance(system_instruction, str) else ""

        if response_schema is not None:
            # This endpoint's guided-decoding support is unconfirmed, so the
            # schema is asked for in-prompt rather than relied on structurally
            # — the same technique any raw-completion (non-Gemini) integration
            # uses when schema-constrained decoding isn't a given.
            raw_schema = (
                response_schema.model_json_schema()
                if isinstance(response_schema, type) and issubclass(response_schema, BaseModel)
                else response_schema
            )
            schema_dict = self._strip_schema_prose(raw_schema)
            top_level_keys = list(schema_dict.get("properties", {}).keys())
            system_text += (
                "\n\nRespond with ONLY a single valid JSON object matching this JSON "
                "Schema, no markdown fences, no commentary, no extra keys. Your JSON "
                f"object's top-level keys must be EXACTLY {top_level_keys} — do not "
                "include the schema itself (no 'properties', 'required', 'type' keys) "
                f"in your answer, only the actual field values:\n{json.dumps(schema_dict)}"
            )

        messages: list[dict] = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.extend(self._content_to_message(c) for c in llm_request.contents)
        return messages

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if stream:
            raise NotImplementedError("MedGemmaEndpointModel does not support streaming.")

        body = {
            "instances": [
                {
                    "@requestFormat": "chatCompletions",
                    "messages": self._build_messages(llm_request),
                    "max_tokens": llm_request.config.max_output_tokens or self.max_output_tokens,
                }
            ]
        }
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.api_base, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                response_text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"MedGemma endpoint returned {resp.status}: {response_text[:2000]}")
                payload = json.loads(response_text)

        try:
            choice = payload["predictions"]["choices"][0]
            message_content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected MedGemma response shape: {payload}") from exc

        # A response cut off by max_tokens is truncated, invalid JSON — surface
        # that plainly instead of letting it fail downstream as a cryptic
        # json.JSONDecodeError / pydantic ValidationError with no clue why.
        if choice.get("finish_reason") == "length":
            raise RuntimeError(
                f"MedGemma response truncated at max_tokens={body['instances'][0]['max_tokens']} "
                "— raise max_output_tokens on this LlmAgent's request config."
            )

        # Real, common raw-completion behavior (unlike Gemini's native
        # structured output, nothing here stops the model wrapping its JSON in
        # a markdown fence) — stripped defensively, not fabricated.
        cleaned = _JSON_FENCE_RE.sub("", message_content.strip()).strip()

        usage = payload.get("predictions", {}).get("usage") or {}
        usage_metadata = (
            types.GenerateContentResponseUsageMetadata(
                prompt_token_count=usage.get("prompt_tokens"),
                candidates_token_count=usage.get("completion_tokens"),
                total_token_count=usage.get("total_tokens"),
            )
            if usage
            else None
        )

        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=cleaned)]),
            finish_reason=types.FinishReason.STOP,
            partial=False,
            usage_metadata=usage_metadata,
        )


def new_medgemma_model() -> MedGemmaEndpointModel:
    if not MEDGEMMA_API_BASE:
        raise RuntimeError(
            "MEDGEMMA_API_BASE is not set — deploy the endpoint first, then set "
            "MEDGEMMA_API_BASE in .env (see tools/gemini_model.py)."
        )
    return MedGemmaEndpointModel(model=MEDGEMMA_MODEL_NAME, api_base=MEDGEMMA_API_BASE)
