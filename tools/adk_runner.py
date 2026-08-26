"""Shared ADK LlmAgent invocation helper — the mechanics of running one
structured-output call through ADK's InMemoryRunner (session creation,
event iteration, extracting the final text, parsing it into the expected
Pydantic model). Originally written once inline for Monitor Agent's
sub-agents; extracted here once Scene Graph Builder needed the exact same
mechanics — real reuse, not speculative.

Invocation mechanics verified directly against the installed ADK 2.6.3:
`InMemoryRunner(agent=...)` + `await runner.session_service.create_session(...)`
+ `runner.run_async(user_id=..., session_id=..., new_message=...)` is the
confirmed local (non-deployed) invocation path, distinct from the Day-1
spike's remote-deployed-agent path (`async_stream_query`).

Deliberately does NOT own concurrency limiting — callers wrap this in
their own semaphore (Monitor's `_GEMINI_CONCURRENCY`, Scene Graph
Builder's own) since each agent's real call volume/cost profile differs
and a shared limiter would either over- or under-constrain one of them.
"""

from __future__ import annotations

import logging
import time

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Real per-call token accounting, appended synchronously (no lock needed —
# asyncio is single-threaded, and the append happens after the awaited call
# returns, never mid-await). Read by scripts/profiling instrumentation that
# imports this module and inspects USAGE_LOG directly, rather than by parsing
# log lines — a real interface, not a scrape of incidental log formatting.
# docs/latency_optimization.md "Fourth pass" §Priority 1 needed the real
# cached_content_token_count per call to answer "is implicit caching firing"
# without assuming it — this is that measurement, left in place rather than
# ripped out once the one-time question was answered, since it costs nothing
# per call and the same question is worth re-asking after any future prompt
# change.
USAGE_LOG: list[dict] = []


async def run_llm_agent_once(
    agent: LlmAgent, content: types.Content, output_model: type[BaseModel], app_name: str
) -> BaseModel:
    runner = InMemoryRunner(agent=agent, app_name=app_name)
    session = await runner.session_service.create_session(app_name=app_name, user_id=app_name)
    final_text: str | None = None
    usage = None
    start = time.monotonic()
    async for event in runner.run_async(user_id=app_name, session_id=session.id, new_message=content):
        # The real ADK Event carries usage_metadata directly (verified against
        # the installed google-adk 2.6.3: Event.model_fields includes
        # usage_metadata: GenerateContentResponseUsageMetadata | None). Kept
        # from the LAST event that actually carries it — intermediate/partial
        # events in a streamed response may have it unset.
        if event.usage_metadata is not None:
            usage = event.usage_metadata
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final_text = part.text
    elapsed = time.monotonic() - start
    if final_text is None:
        raise RuntimeError(f"{agent.name} produced no text output")

    logger.info("adk_runner[%s]: gemini call took %.2fs", agent.name, elapsed)
    cached = getattr(usage, "cached_content_token_count", None) if usage else None
    prompt = getattr(usage, "prompt_token_count", None) if usage else None
    total = getattr(usage, "total_token_count", None) if usage else None
    USAGE_LOG.append(
        {"agent": agent.name, "cached_content_token_count": cached, "prompt_token_count": prompt, "total_token_count": total}
    )
    logger.info(
        "adk_runner[%s]: cached_content_token_count=%s prompt_token_count=%s total_token_count=%s",
        agent.name, cached, prompt, total,
    )

    return output_model.model_validate_json(final_text)
