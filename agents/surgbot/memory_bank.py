"""This project's first real production use of GEAP Memory Bank.

Every other GEAP-adjacent mention in the codebase before SurgBot (initial_
11082026.md, state/schema.py's docstring) is aspirational — Firestore has
always been the actual substitute. This module calls the real thing: the
identical `vertexai.Client().agent_engines` surface already used for
deployment (scripts/spike_deploy_stub_agent.py, scripts/deploy_surgbot_
subagents.py) also exposes create_memory/retrieve_memories/list_memories/
generate_memories — confirmed present on that client object this session via
direct introspection (`dir(client.agent_engines)`).

create_memory(reviewer_id, fact) fires on Phase 5 approval (root_agent.py's
draft_review_document / the approval endpoint in services/surgbot_service),
scoped {agent_name: "surgbot", user: reviewer_id} so Phase 6's retrieval can
find "this reviewer's own past review patterns" specifically, not every
memory ever written by every reviewer.

retrieve_memories(reviewer_id, query) backs Phase 6's cross-session pattern
review — NEVER fabricated: if Memory Bank returns zero memories for a
reviewer (e.g. their first-ever session), that is the honest answer and
subagents.py's pattern_insight subagent must be told exactly that, not fed a
synthesized "3 of 4 past sessions" claim with nothing real behind it.

Memory Bank requires its own scope — a `google.genai.types.AgentEngineMemory`
-shaped `create_memory` call needs the parent Agent Engine resource these
memories are scoped under. This module targets the deployed SYNTHESIS
subagent's resource name (agents/surgbot/subagents.py's cached deployment) as
that parent, since synthesis is the subagent whose Phase 5/6 output this
memory record actually informs — not the root Live agent, whose deployment
identity is unrelated to what gets remembered across sessions.
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


def create_memory(reviewer_id: str, fact: str, agent_engine: str) -> str | None:
    """Writes one durable fact about a reviewer's review pattern to Memory
    Bank, scoped to the given deployed Agent Engine resource name (the
    parent every memory must be created under). Returns the created memory's
    resource name, or None if the call failed — callers must not treat a
    failed memory write as fatal to the review-approval flow it's attached
    to (Phase 5 approval must still succeed even if this one side-effect
    doesn't)."""
    try:
        operation = _get_client().agent_engines.create_memory(
            name=agent_engine,
            fact=fact,
            scope={"agent_name": "surgbot", "user_id": reviewer_id},
        )
        # create_memory returns an AgentEngineMemoryOperation (a long-running
        # operation envelope: name/done/error/response) — confirmed via
        # direct signature+field introspection this session, not assumed.
        # The actual Memory resource, if the op completed synchronously, is
        # under .response; fall back to the operation's own name otherwise.
        response = getattr(operation, "response", None)
        resource_name = getattr(response, "name", None) or getattr(operation, "name", None)
        logger.info("surgbot memory_bank: created memory %s for reviewer %s", resource_name, reviewer_id)
        return resource_name
    except Exception:
        logger.exception("surgbot memory_bank: create_memory failed for reviewer %s — continuing without it", reviewer_id)
        return None


def retrieve_memories(reviewer_id: str, query: str, agent_engine: str, top_k: int = 5) -> list[str]:
    """Returns real, retrieved memory facts for this reviewer — never a
    fabricated list. An empty result (no memories yet, or a call failure) is
    returned as an empty list, which subagents.py's pattern_insight subagent
    must render honestly ("no prior session history yet"), not paper over."""
    try:
        # retrieve_memories returns an Iterator[RetrieveMemoriesResponse
        # RetrievedMemory] directly (confirmed via signature introspection
        # this session) — each item has .memory.fact and .distance, not a
        # wrapping .retrieved_memories attribute.
        results = _get_client().agent_engines.retrieve_memories(
            name=agent_engine,
            scope={"agent_name": "surgbot", "user_id": reviewer_id},
            similarity_search_params={"search_query": query, "top_k": top_k},
        )
        facts: list[str] = []
        for item in results:
            memory = getattr(item, "memory", None)
            fact = getattr(memory, "fact", None) if memory is not None else None
            if fact:
                facts.append(fact)
        return facts
    except Exception:
        logger.exception("surgbot memory_bank: retrieve_memories failed for reviewer %s — returning empty, not fabricated", reviewer_id)
        return []
