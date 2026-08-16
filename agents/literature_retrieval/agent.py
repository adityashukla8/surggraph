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

import asyncio
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


# Per-query cap before merging. Europe PMC's own relevance ranking is fine at
# this scale — it only breaks when an over-constrained AND has starved it of
# candidates, which is what the decomposition upstream exists to prevent.
PER_QUERY_LIMIT = 25

# Reciprocal Rank Fusion constant. This is the published default from Cormack,
# Clarke & Buettcher (SIGIR 2009), not a number chosen here — RRF is the
# standard way to merge ranked lists from different queries without needing
# comparable scores between them, which matters because Europe PMC returns no
# score at all for a live search.
RRF_K = 60


async def retrieve(
    case_id: str,
    queries: list[str],
    top_n: int = DEFAULT_TOP_N,
    parent_node_id: str | None = None,
) -> tuple[list[dict], list[str], bool]:
    """Runs several short queries in parallel and merges their results.

    One long query does not rank badly on this API — it starves the ranker.
    Every term is an AND clause, so a precise-sounding query eliminates the
    papers it was meant to find. Several short queries along independent axes
    each return real candidates, and merging them is where the precision comes
    back: a paper surfaced by more than one angle is almost certainly on topic.

    Ranking is Reciprocal Rank Fusion, which needs only each paper's POSITION
    within each result list. That matters because a live Europe PMC search
    returns no relevance score, so there is nothing to average or normalise.

    Returns (hits, node_ids, evidence_available).
    """
    wanted = [q for q in queries if q and q.strip()]
    if not wanted:
        return [], [], False

    # Genuinely concurrent — these are independent network calls, and running
    # four sequentially would add seconds to a path that already waits on two
    # Gemini calls.
    results = await asyncio.gather(
        *(asyncio.to_thread(_search_one, case_id, q) for q in wanted), return_exceptions=True
    )

    ranked_lists: list[list[dict]] = []
    for query, result in zip(wanted, results):
        if isinstance(result, Exception):
            logger.warning("literature[%s]: query %r failed: %s", case_id, query[:60], type(result).__name__)
            continue
        logger.info("literature[%s]: %d hit(s) for %r", case_id, len(result), query[:60])
        ranked_lists.append(result)

    merged = _reciprocal_rank_fusion(ranked_lists)[:top_n]
    if not merged:
        return [], [], False

    node_ids_written = await _write_literature_nodes(case_id, merged, parent_node_id)
    logger.info(
        "literature[%s]: %d queries -> %d unique papers -> top %d (%d hit by >1 query)",
        case_id,
        len(ranked_lists),
        sum(len(r) for r in ranked_lists),
        len(merged),
        sum(1 for h in merged if h["_query_count"] > 1),
    )
    return merged, node_ids_written, True


def _search_one(case_id: str, query: str) -> list[dict]:
    """One query, cached per case. Blocking — the caller runs these in threads."""
    cache_key = (case_id, query_hash(query))
    if cache_key in _cache:
        return _cache[cache_key]
    hits = search_literature(query, k=PER_QUERY_LIMIT, live=True).get("hits", [])
    for hit in hits:
        hit["query_used"] = query
    _cache[cache_key] = hits
    return hits


def _paper_key(hit: dict) -> str:
    """Identity across queries. The same paper must collapse to one entry or
    the whole point of boosting multi-query hits is lost."""
    return str(hit.get("doc_id") or hit.get("pmcid") or hit.get("url") or hit.get("title", ""))


def _reciprocal_rank_fusion(ranked_lists: list[list[dict]]) -> list[dict]:
    """Merges ranked lists by summed reciprocal rank.

    A paper at position 1 of one list scores 1/(60+1); appearing in three lists
    accumulates three such terms. So a paper found by several independent
    angles outranks one that topped a single list — which is exactly the signal
    we want, since agreement between differently-phrased queries is the best
    available evidence of topicality when no relevance score exists.
    """
    scores: dict[str, float] = {}
    best: dict[str, dict] = {}
    counts: dict[str, int] = {}
    queries_hit: dict[str, list[str]] = {}

    for hits in ranked_lists:
        for rank, hit in enumerate(hits, start=1):
            key = _paper_key(hit)
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
            counts[key] = counts.get(key, 0) + 1
            queries_hit.setdefault(key, []).append(hit.get("query_used", ""))
            best.setdefault(key, hit)

    merged = []
    for key, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        hit = dict(best[key])
        hit["_rrf_score"] = round(score, 5)
        hit["_query_count"] = counts[key]
        hit["_queries_hit"] = queries_hit[key]
        merged.append(hit)
    return merged


async def _write_literature_nodes(case_id: str, hits: list[dict], parent_node_id: str | None) -> list[str]:
    # Papers hang off WHATEVER PROMPTED THE SEARCH — the error under
    # investigation — not off the Literature Retrieval agent. Parenting them to
    # the agent made it a large competing hub on screen and buried the sequence
    # a reader actually follows: error -> literature -> complication.
    #
    # A `hierarchy` edge here is deliberately distinct from an `evidence` edge.
    # Retrieved means consulted; evidence means it actually supports the claim
    # that cites it.
    parent = parent_node_id or node_ids.agent(SOURCE_AGENT)
    patches: list[tuple] = []
    written_ids: list[str] = []

    for i, hit in enumerate(hits):
        node_id = node_ids.literature_evidence(query_hash(_paper_key(hit)), i)
        written_ids.append(node_id)
        patches.append(
            (
                GraphNodePatch(
                    node_id=node_id,
                    node_type="literature_evidence",
                    label=_clean(hit.get("title"))[:120] or "(untitled)",
                    attrs={
                        "pmcid": hit.get("pmcid"),
                        "doc_id": hit.get("doc_id"),
                        "url": hit.get("url"),
                        "journal": hit.get("journal"),
                        "year": hit.get("year"),
                        "snippet": _clean(hit.get("snippet"))[:600],
                        # Which queries surfaced this, and how many. A paper
                        # found by several independent angles is a stronger
                        # candidate, and that is auditable rather than implied.
                        "queries_hit": hit.get("_queries_hit", []),
                        "query_count": hit.get("_query_count", 1),
                        "rrf_score": hit.get("_rrf_score"),
                        "retrieved_live": True,
                    },
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                f"Retrieved by {hit.get('_query_count', 1)} quer(y/ies)",
            )
        )
        patches.append(
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(parent, node_id, "hierarchy"),
                    source_node_id=parent,
                    target_node_id=node_id,
                    edge_kind="hierarchy",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                    reason="Consulted during this investigation",
                ),
                "Consulted during this investigation",
            )
        )

    await apply_state_patches(case_id, patches)
    return written_ids


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
