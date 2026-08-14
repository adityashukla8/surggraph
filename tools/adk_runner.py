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

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel


async def run_llm_agent_once(
    agent: LlmAgent, content: types.Content, output_model: type[BaseModel], app_name: str
) -> BaseModel:
    runner = InMemoryRunner(agent=agent, app_name=app_name)
    session = await runner.session_service.create_session(app_name=app_name, user_id=app_name)
    final_text: str | None = None
    async for event in runner.run_async(user_id=app_name, session_id=session.id, new_message=content):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final_text = part.text
    if final_text is None:
        raise RuntimeError(f"{agent.name} produced no text output")
    return output_model.model_validate_json(final_text)
