"""Generic GEAP Memory Bank access — create_memory/retrieve_memories against
an arbitrary exact-match `scope: dict[str, str]` and a caller-supplied parent
Agent Engine resource name. Nothing SurgBot-specific lives here; this module
knows nothing about reviewers, cases, or feedback.

MOVED HERE FROM agents/surgbot/memory_bank.py (plan_v2 §16.9 Step 3): this
project's own layering convention is agents/ -> tools/, never tools/ ->
agents/ (verified by grep across the repo before this move — no exception
existed anywhere). tools/feedback_kb.py, the SurgGraph-agent-facing read
path for the surgeon-feedback knowledge base, needs Memory Bank access; by
the time that need came up, this module's two functions had already been
made fully generic (plan_v2 §16.1c/§16.2 — an explicit `scope` dict, no
`reviewer_id` parameter anywhere), so moving it here rather than duplicating
~50 lines of real Vertex API client code into tools/feedback_kb.py was the
straightforward fix, not a shortcut around one.

This project's first real production use of GEAP Memory Bank (originally
built for SurgBot). Every other GEAP-adjacent mention in the codebase before
that (initial_11082026.md, state/schema.py's docstring) is aspirational —
Firestore has always been the actual substitute. This module calls the real
thing: the identical `vertexai.Client().agent_engines` surface already used
for deployment (scripts/spike_deploy_stub_agent.py, scripts/deploy_surgbot_
subagents.py) also exposes create_memory/retrieve_memories/list_memories/
generate_memories — confirmed present on that client object via direct
introspection (`dir(client.agent_engines)`).

`scope` is an exact-match filter (confirmed against the installed SDK
directly, `memories.py`'s own docstring: "A memory must have exactly the
same scope as the scope provided here to be retrieved... same keys and
values", re-confirmed against Google's current Memory Bank docs), so it
doubles as a real routing key — this is what makes both real callers work:
  - agents/surgbot/feedback.py — the surgeon-feedback knowledge base
    (plan_v2 §16), scoped by {agent_name, user_id: <constant KB id>,
    target_agent, kind}, and read back by tools/feedback_kb.py using the
    same scope shape.
  - agents/surgbot/root_agent.py's retrieve_reviewer_patterns (Phase 6),
    scoped by feedback.REVIEW_SUMMARY_SCOPE.

query=None means simple (non-similarity) retrieval — confirmed via Google's
current docs to be exactly "call retrieve() with scope and omit
similarity_search_params", not a separate parameter — used for standing
directives, which must always come back regardless of how well they match
the current situation; query set means similarity-search retrieval, used
for case-grounded observations. NEVER fabricated either way: a Memory Bank
failure or a genuinely empty scope returns an empty list / None, which
callers must render honestly, not paper over with a synthesized answer.

Memory Bank requires its own scope — a `google.genai.types.AgentEngineMemory`
-shaped `create_memory` call needs the parent Agent Engine resource these
memories are scoped under. Callers pass this explicitly (`agent_engine`);
this module has no opinion on which deployed engine that should be.
"""

from __future__ import annotations

import logging
import os

import vertexai
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
REGION = os.environ.get("SURGGRAPH_REGION", "us-central1")

_client: vertexai.Client | None = None


def _get_client() -> vertexai.Client:
    global _client
    if _client is None:
        _client = vertexai.Client(project=PROJECT_ID, location=REGION)
    return _client


def create_memory(fact: str, scope: dict[str, str], agent_engine: str) -> str | None:
    """Writes one durable fact to Memory Bank under the given scope (an
    exact-match filter — see module docstring), scoped to the given deployed
    Agent Engine resource name (the parent every memory must be created
    under). Returns the created memory's resource name, or None if the call
    failed — callers must not treat a failed memory write as fatal to
    whatever flow it's attached to (a review approval, or a feedback-KB
    write, must still succeed even if this one side-effect doesn't)."""
    try:
        operation = _get_client().agent_engines.create_memory(
            name=agent_engine,
            fact=fact,
            scope=scope,
        )
        # create_memory returns an AgentEngineMemoryOperation (a long-running
        # operation envelope: name/done/error/response) — confirmed via
        # direct signature+field introspection this session, not assumed.
        # The actual Memory resource, if the op completed synchronously, is
        # under .response; fall back to the operation's own name otherwise.
        response = getattr(operation, "response", None)
        resource_name = getattr(response, "name", None) or getattr(operation, "name", None)
        logger.info("memory_bank: created memory %s under scope %s", resource_name, scope)
        return resource_name
    except Exception:
        logger.exception("memory_bank: create_memory failed for scope %s — continuing without it", scope)
        return None


def retrieve_memories(scope: dict[str, str], agent_engine: str, query: str | None = None, top_k: int = 5) -> list[str]:
    """Returns real, retrieved memory facts under the given scope — never a
    fabricated list. `query=None` is SIMPLE retrieval (every memory in
    scope, unranked — confirmed against Google's current docs to be exactly
    "omit similarity_search_params", not a separate call shape); a `query`
    runs similarity-search retrieval instead. An empty result (no memories
    yet, or a call failure) is returned as an empty list, which every caller
    must render honestly ("no matching feedback"/"no prior session history
    yet"), never papered over with a synthesized answer."""
    try:
        kwargs: dict = {"name": agent_engine, "scope": scope}
        if query is not None:
            kwargs["similarity_search_params"] = {"search_query": query, "top_k": top_k}
        # retrieve_memories returns an Iterator[RetrieveMemoriesResponse
        # RetrievedMemory] directly (confirmed via signature introspection
        # this session) — each item has .memory.fact and .distance, not a
        # wrapping .retrieved_memories attribute.
        results = _get_client().agent_engines.retrieve_memories(**kwargs)
        facts: list[str] = []
        for item in results:
            memory = getattr(item, "memory", None)
            fact = getattr(memory, "fact", None) if memory is not None else None
            if fact:
                facts.append(fact)
        return facts
    except Exception:
        logger.exception("memory_bank: retrieve_memories failed for scope %s — returning empty, not fabricated", scope)
        return []
