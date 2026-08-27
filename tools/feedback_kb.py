"""Surgeon-feedback knowledge base — shared contract + the SurgGraph-agent
read path (plan_v2 §16).

WHY THIS LIVES IN tools/, NOT agents/surgbot/: this project's own layering
is one-directional — agents/ imports from tools/, never the reverse
(verified by grep across the whole repo before this module was written; no
existing exception). The four SurgGraph agents that consult feedback
(divergence_detection, complication_reasoning, literature_retrieval,
corrective_replanning) build their prompts under agents/, so the read path
has to live somewhere they can import without inverting that direction.
Since the SCOPE SHAPE, the FACT FORMAT, and the ROUTING TABLE are a contract
shared between SurgBot's write path (agents/surgbot/feedback.py) and this
read path — not something either side owns alone — they live here too, and
agents/surgbot/feedback.py imports them from here rather than the other way
around. This is the "bounded knowledge source consulted at inference time"
pattern this codebase already uses (agents/corrective_replanning/library.py,
tools/europepmc_rag.py), sized for the fact that FOUR different agents
consult it, not one.

ADVISORY ONLY (locked decision, plan_v2 §16.0): feedback_block() returns
prompt text, nothing else. No agent's behavior is gated, suppressed, or
auto-tuned by anything in this module. The rendered block says so explicitly
so a reader of the actual prompt sees the same constraint the code enforces.

INSTITUTION-WIDE BY A CONSTANT SCOPE (plan_v2 §16.1c): SURGGRAPH_KB_USER_ID
is a Memory Bank scope value only, never a Firestore reviewer_id — see
agents/surgbot/schema.py::FeedbackRecord's docstring for the real-identity
side of this.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Literal

from dotenv import load_dotenv

from state.node_ids import node_type_for
from tools import memory_bank

load_dotenv()

logger = logging.getLogger(__name__)

# --- Scope (§16.1c / §16.2) ----------------------------------------------------

# A Memory Bank scope value ONLY — see module docstring. Never a Firestore
# reviewer_id.
SURGGRAPH_KB_USER_ID = "1"

SCOPE_ROOT = {"agent_name": "surggraph", "user_id": SURGGRAPH_KB_USER_ID}


def directive_scope(target_agent: str) -> dict[str, str]:
    return {**SCOPE_ROOT, "target_agent": target_agent, "kind": "directive"}


def observation_scope(target_agent: str) -> dict[str, str]:
    return {**SCOPE_ROOT, "target_agent": target_agent, "kind": "observation"}


# Phase 6's cross-session pattern review (agents/surgbot/root_agent.py::
# retrieve_reviewer_patterns) — same constant-scope reasoning: a per-reviewer
# scope meant anyone but the single heaviest user saw an empty pattern
# history.
REVIEW_SUMMARY_SCOPE = {**SCOPE_ROOT, "kind": "review_summary"}


# --- Routing (§16.2) ------------------------------------------------------------

# Real node_type values from state/schema.py's NodeType, mapped to the
# SurgGraph agent whose behavior that kind of finding is actually about.
# "error" is intentionally routed to error_detection even though that agent
# does not consume feedback in v1 (§16.7) — feedback on error nodes is still
# captured and stored, ready the day that agent's construction changes.
NODE_TYPE_TO_AGENT: dict[str, str] = {
    "divergence_alert": "divergence_detection",
    "complication": "complication_reasoning",
    "literature_evidence": "literature_retrieval",
    "corrective_trajectory": "corrective_replanning",
    "error": "error_detection",
}

TargetAgent = Literal[
    "divergence_detection", "complication_reasoning", "literature_retrieval", "corrective_replanning", "error_detection"
]


def target_agent_for(subject_node_id: str) -> tuple[str | None, str | None]:
    """Returns (node_type, target_agent) for a feedback item's subject node.
    Both None if subject_node_id is empty, follows no known id convention,
    or maps to a node_type not in the routing table above — never guessed.

    Reuses state/node_ids.py::node_type_for, the single source of truth for
    id -> node_type in this codebase, rather than re-deriving id prefixes by
    hand — the exact convention-drift risk that module's own docstring warns
    against.
    """
    if not subject_node_id:
        return None, None
    node_type = node_type_for(subject_node_id)
    if node_type is None:
        return None, None
    return node_type, NODE_TYPE_TO_AGENT.get(node_type)


# --- Fact format (§16.2) --------------------------------------------------------

_FACT_HEADER_RE = re.compile(
    r"^\[verdict=(?P<verdict>[^|\]]*)\s*\|\s*node_type=(?P<node_type>[^|\]]*)\s*\|\s*"
    r"case=(?P<case>[^|\]]*)\s*\|\s*at=(?P<at>[^\]]*)\]\n(?P<body>.*)$",
    re.DOTALL,
)


@dataclass
class FeedbackFact:
    verdict: str | None
    node_type: str | None
    case_id: str | None
    at: str | None
    body: str


def format_fact(*, verdict: str | None, node_type: str | None, case_id: str | None, at: str, body: str) -> str:
    """Structured but still a plain string, since Memory Bank facts are
    strings — parse_fact reads the header back for rendering. A malformed or
    hand-written fact with no header still round-trips as plain body text
    (parse_fact never raises)."""
    header = f"[verdict={verdict or ''} | node_type={node_type or ''} | case={case_id or ''} | at={at}]"
    return f"{header}\n{body}"


def parse_fact(fact: str) -> FeedbackFact:
    match = _FACT_HEADER_RE.match(fact)
    if not match:
        return FeedbackFact(verdict=None, node_type=None, case_id=None, at=None, body=fact)
    # The header's `[^|\]]*` capture groups are greedy and include the space
    # before the next ` | ` separator (found via a real failing test, not
    # assumed) — .strip() each one rather than fighting the regex boundary.
    groups = {key: value.strip() for key, value in match.groupdict().items()}
    return FeedbackFact(
        verdict=groups["verdict"] or None,
        node_type=groups["node_type"] or None,
        case_id=groups["case"] or None,
        at=groups["at"] or None,
        body=groups["body"],
    )


# --- Read path (§16.4) -----------------------------------------------------------

# Bounds so an injected block can never crowd out the actual case context
# it's advisory to (plan_v2 §16.4's "bounded" property).
MAX_DIRECTIVES = 5
MAX_OBSERVATIONS = 3
MAX_ITEM_CHARS = 400

# Divergence Detection polls repeatedly while a proposal is live — re-
# fetching Memory Bank on every poll would add real, avoidable latency for
# content that only changes when a review is approved. Short TTL, not "never
# refetch": a demo where feedback is approved mid-case should still show up
# within a few minutes, not require a restart.
_CACHE_TTL_S = 300.0
_cache: dict[tuple[str, str, str], tuple[float, list[str]]] = {}
_cache_hits = 0
_cache_misses = 0


def _cached_retrieve(scope: dict[str, str], agent_engine: str, query: str | None, top_k: int) -> list[str]:
    global _cache_hits, _cache_misses
    key = (agent_engine, str(sorted(scope.items())), query or "")
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_S:
        _cache_hits += 1
        return cached[1]
    _cache_misses += 1
    facts = memory_bank.retrieve_memories(scope, agent_engine, query, top_k)
    _cache[key] = (now, facts)
    return facts


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_ITEM_CHARS else text[: MAX_ITEM_CHARS - 1] + "…"


def default_agent_engine() -> str | None:
    """The Memory Bank parent Agent Engine resource, read from
    SURGGRAPH_FEEDBACK_KB_ENGINE — deliberately an env var, not an import of
    agents/surgbot/feedback.py: that would make the four core pipeline
    agents that call feedback_block() depend on SurgBot's deployment
    internals, exactly backwards from this project's one-directional design
    (SurgBot reads the pipeline, never the reverse). Returns None if unset
    — feedback_block() then fails soft to an empty block, same as any other
    misconfiguration or outage, never breaking a case run."""
    return os.environ.get("SURGGRAPH_FEEDBACK_KB_ENGINE") or None


async def feedback_block(target_agent: str, context_query: str, agent_engine: str | None = None, top_k: int = 3) -> str:
    """Returns prompt-ready advisory text for `target_agent`, or "" when
    there is nothing to say — callers must be byte-identical to their
    pre-feedback behavior in that case (plan_v2 §16.5's structural
    requirement). Two concurrent Memory Bank retrievals: every standing
    directive for this agent (simple retrieval — directives always apply,
    regardless of how well they match `context_query`), and the top_k
    observations most similar to `context_query` (similarity search).

    `agent_engine` defaults to default_agent_engine() (env-resolved); pass
    it explicitly only for tests that need a specific/fake engine.

    Fails soft, always: tools/memory_bank.py's own functions already return
    [] on any error rather than raising, so a GEAP outage degrades this to
    an empty block, never an exception that could break a case run. An
    unconfigured engine (no env var set) is the same story — logged once,
    not raised.
    """
    if agent_engine is None:
        agent_engine = default_agent_engine()
    if not agent_engine:
        logger.info("feedback_kb[%s]: SURGGRAPH_FEEDBACK_KB_ENGINE not set — no block", target_agent)
        return ""
    start = time.monotonic()
    try:
        directives, observations = await asyncio.gather(
            asyncio.to_thread(_cached_retrieve, directive_scope(target_agent), agent_engine, None, MAX_DIRECTIVES),
            asyncio.to_thread(_cached_retrieve, observation_scope(target_agent), agent_engine, context_query, top_k),
        )
    except Exception:
        logger.exception("feedback_kb[%s]: retrieval failed — returning no block, agent runs unaffected", target_agent)
        return ""

    directives = directives[:MAX_DIRECTIVES]
    observations = observations[:MAX_OBSERVATIONS]
    elapsed = time.monotonic() - start
    logger.info(
        "feedback_kb[%s]: %d directive(s), %d observation(s) in %.2fs",
        target_agent, len(directives), len(observations), elapsed,
    )

    if not directives and not observations:
        return ""

    lines = ["REVIEWER FEEDBACK — advisory input from past case reviews, NOT ground truth."]
    if directives:
        lines.append("Standing guidance:")
        for d in directives:
            lines.append(f"  - {_truncate(parse_fact(d).body)}")
    if observations:
        lines.append("Similar past reviews:")
        for o in observations:
            parsed = parse_fact(o)
            tag_parts = [p for p in (parsed.verdict, parsed.node_type, parsed.case_id) if p]
            tag = " · ".join(tag_parts)
            prefix = f"[{tag}] " if tag else ""
            lines.append(f"  - {prefix}{_truncate(parsed.body)}")
    lines.append(
        "You MUST still report what you actually observe. Do not suppress a finding "
        "solely because a past reviewer disagreed with a similar one; you may adjust "
        "your confidence and say why you did."
    )
    return "\n".join(lines)
