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
import logging

from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch
from tools.europepmc_rag import search_literature
from tools.state_tools import apply_state_patches

logger = logging.getLogger(__name__)

SOURCE_AGENT = "literature_retrieval"
_SOURCE_TOOL = "retrieve_literature"

DEFAULT_TOP_N = 4

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


async def retrieve(case_id: str, query: str, top_n: int = DEFAULT_TOP_N) -> tuple[list[dict], list[str], bool]:
    """Runs one retrieval and writes its results as literature_evidence nodes.

    Returns (hits, node_ids, evidence_available). `evidence_available` is False
    when the search genuinely failed or returned nothing — deliberately
    distinct from "we did not look", so a downstream reasoner can tell an
    unsupported claim from an unexamined one.
    """
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

    patches = []
    written_ids = []
    for i, hit in enumerate(hits[:top_n]):
        node_id = node_ids.literature_evidence(qhash, i)
        written_ids.append(node_id)
        patches.append(
            (
                GraphNodePatch(
                    node_id=node_id,
                    node_type="literature_evidence",
                    label=hit.get("title", "(untitled)")[:120],
                    attrs={
                        "pmcid": hit.get("pmcid"),
                        "pmid": hit.get("pmid"),
                        "doi": hit.get("doi"),
                        "url": hit.get("url"),
                        "journal": hit.get("journal"),
                        "year": hit.get("year"),
                        "snippet": hit.get("abstract", "")[:600],
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
