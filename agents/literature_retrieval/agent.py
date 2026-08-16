"""Literature Retrieval Agent — docs/agentic_workflow.md §3 agent 5.

Not an LLM agent. It is a tool wrapper around one Europe PMC call, treated as
an agent for graph-provenance and observability reasons: every citation that
ends up supporting a complication or a corrective proposal has to be traceable
to a real retrieval with a real query, and giving that retrieval its own agent
node and its own graph writes is what makes the evidence chain inspectable
rather than asserted.

THE QUERY IS NEVER FORMULATED HERE. The caller — Complication Reasoning, or
Corrective Replanning when it needs evidence beyond what the complication
already carries — composes the query from live case context. Nothing in this
module maps an error category to a search term, because that mapping is exactly
the hand-authored lookup table the design forbids. This module takes a string
and returns papers.

CACHING IS PER CASE AND KEYED BY QUERY. Two different errors in one case
frequently reason toward the same literature; re-fetching costs a network round
trip and returns identical results. The cache is keyed by the hash of the query
text, so a genuinely different question always goes out to the network.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re

from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch
from tools.europepmc_rag import search_literature
from tools.state_tools import apply_state_patches

logger = logging.getLogger(__name__)

SOURCE_AGENT = "literature_retrieval"
_SOURCE_TOOL = "retrieve_literature"

DEFAULT_TOP_N = 4

# Result ordering is left to Europe PMC's default, which IS relevance —
# measured, not assumed. An explicit `sort=CITED desc` was tried against a real
# clinical query and returned strictly worse matches (older, more tangential
# papers), so there is nothing to gain by overriding it. Retrieval quality here
# is a function of the query, which is the agent's to compose.

# Europe PMC returns titles and snippets containing real markup: HTML entities
# (&lt;sup&gt;) and inline tags (<i>, <sup>) used for typesetting. Left in, that
# noise lands verbatim in a reasoning prompt and in a graph node's label. It is
# presentation, not content, so it is stripped once here rather than in every
# consumer.
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str | None) -> str:
    if not text:
        return ""
    # Unescape first: entities can decode INTO tags (&lt;sup&gt; -> <sup>),
    # so stripping before unescaping would leave them behind.
    return _TAG_RE.sub("", html.unescape(text)).strip()

# Per-case, per-query cache: {(case_id, query_hash): [hit, ...]}. In-process is
# the right scope — one case is owned by one orchestrator task, and the cache's
# whole purpose is to avoid repeat fetches WITHIN a case.
_cache: dict[tuple[str, str], list[dict]] = {}

# Retry policy from docs §9: two attempts, then an empty result. Retrieval
# failing is not fatal — the caller proceeds and marks the complication
# evidence-unavailable, which the verification gate then refuses to build an
# external alert on.
_MAX_ATTEMPTS = 2


def query_hash(query: str) -> str:
    return hashlib.sha256(query.strip().lower().encode()).hexdigest()[:12]


async def retrieve(
    case_id: str, query: str, top_n: int = DEFAULT_TOP_N, fallbacks: list[str] | None = None
) -> tuple[list[dict], list[str], bool]:
    """Runs a retrieval and writes its results as literature_evidence nodes.

    `fallbacks` are progressively broader phrasings, tried in order only if the
    primary returns nothing. Europe PMC requires every term, so a precise
    clinical query can legitimately match zero papers while a broader one
    matching the same question returns several — measured, not assumed.

    Returns (hits, node_ids, evidence_available). `evidence_available` is False
    when every attempt genuinely returned nothing — deliberately distinct from
    "we did not look", so a downstream reasoner can tell an unsupported claim
    from an unexamined one.
    """
    for candidate in [query, *(fallbacks or [])]:
        if not candidate or not candidate.strip():
            continue
        hits, ids, ok = await _retrieve_one(case_id, candidate, top_n)
        if ok:
            if candidate != query:
                logger.info("literature[%s]: primary query found nothing; %r did", case_id, candidate[:60])
            return hits, ids, ok
    return [], [], False


async def _retrieve_one(case_id: str, query: str, top_n: int) -> tuple[list[dict], list[str], bool]:
    qhash = query_hash(query)
    cache_key = (case_id, qhash)

    if cache_key in _cache:
        hits = _cache[cache_key]
        logger.info("literature[%s]: cache hit for %r (%d results)", case_id, query[:60], len(hits))
    else:
        hits = []
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                # live=True: the point is on-demand retrieval against real
                # current literature for the question the agent actually
                # formulated, not a lookup in a corpus pre-seeded with
                # questions someone anticipated.
                result = search_literature(query, k=top_n, live=True)
                hits = result.get("hits", [])
                break
            except Exception:
                logger.warning("literature[%s]: attempt %d/%d failed for %r", case_id, attempt, _MAX_ATTEMPTS, query[:60])
        else:
            logger.exception("literature[%s]: retrieval exhausted for %r", case_id, query[:60])
        _cache[cache_key] = hits

    if not hits:
        return [], [], False

    # The agent node owns every paper it retrieved, whether or not a claim ends
    # up citing it. Without this the unused results sit on the graph as orphans
    # — visible papers connected to nothing, which reads as noise rather than
    # as "these were consulted and found not to support the claim."
    #
    # This is deliberately distinct from an `evidence` edge. Retrieved means
    # consulted; evidence means it actually supports the claim that cites it.
    # Collapsing the two would make the evidence edges meaningless.
    patches = [
        (
            GraphNodePatch(
                node_id=node_ids.agent(SOURCE_AGENT),
                node_type="agent",
                label="Literature Retrieval",
                source_agent=SOURCE_AGENT,
                source_tool=_SOURCE_TOOL,
            ),
            None,
            "Literature Retrieval registered for this case",
        )
    ]
    written_ids = []
    for i, hit in enumerate(hits[:top_n]):
        node_id = node_ids.literature_evidence(qhash, i)
        written_ids.append(node_id)
        patches.append(
            (
                GraphNodePatch(
                    node_id=node_id,
                    node_type="literature_evidence",
                    label=_clean(hit.get("title")) [:120] or "(untitled)",
                    attrs={
                        "pmcid": hit.get("pmcid"),
                        "pmid": hit.get("pmid"),
                        "doi": hit.get("doi"),
                        "url": hit.get("url"),
                        "journal": hit.get("journal"),
                        "doc_id": hit.get("doc_id"),
                        "year": hit.get("year"),
                        # The API's field is `snippet`, not `abstract`.
                        "snippet": _clean(hit.get("snippet"))[:600],
                        # The query is stored ON the node: an evidence node
                        # whose originating question is lost cannot be audited
                        # for whether it actually supports what cites it.
                        "query_used": query,
                        "query_hash": qhash,
                        "retrieved_live": True,
                    },
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                f"Retrieved for query: {query[:100]}",
            )
        )
        patches.append(
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(node_ids.agent(SOURCE_AGENT), node_id, "hierarchy"),
                    source_node_id=node_ids.agent(SOURCE_AGENT),
                    target_node_id=node_id,
                    edge_kind="hierarchy",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                    reason=f"Consulted for: {query[:80]}",
                ),
                f"Consulted for: {query[:80]}",
            )
        )

    await apply_state_patches(case_id, patches)
    logger.info("literature[%s]: %d result(s) for %r", case_id, len(written_ids), query[:60])
    return hits, written_ids, True


def evidence_edges(literature_node_ids: list[str], target_node_id: str, reason: str) -> list[tuple]:
    """Evidence edges from each citation to whatever it supports.

    Direction is literature -> claim, matching §4.2: the paper is the source of
    support and the complication or corrective proposal is what it supports.
    """
    return [
        (
            None,
            GraphEdgePatch(
                edge_id=node_ids.edge(lit_id, target_node_id, "evidence"),
                source_node_id=lit_id,
                target_node_id=target_node_id,
                edge_kind="evidence",
                source_agent=SOURCE_AGENT,
                source_tool=_SOURCE_TOOL,
                reason=reason,
            ),
            reason,
        )
        for lit_id in literature_node_ids
    ]


async def ensure_agent_node(case_id: str) -> None:
    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=node_ids.agent(SOURCE_AGENT),
                    node_type="agent",
                    label="Literature Retrieval",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                "Literature Retrieval registered for this case",
            )
        ],
    )
