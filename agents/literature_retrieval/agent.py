"""Literature Retrieval Agent — docs/agentic_workflow.md §3 agent 5.

Not an LLM agent. It is a tool wrapper around three independent literature
APIs (Europe PMC, PubMed E-utilities, Semantic Scholar Graph API), treated as
an agent for graph-provenance and observability reasons: every citation that
ends up supporting a complication or a corrective proposal has to be traceable
to a real retrieval with a real query, and giving that retrieval its own agent
node and its own graph writes is what makes the evidence chain inspectable
rather than asserted.

THREE SOURCES, ONE QUERY, DIFFERENT SYNTAX EACH. The calling agent composes
one query set in Europe PMC's boolean-field syntax (see
agents/complication_reasoning/subagent.py's _QUERY_INSTRUCTION). Rather than
teach it three query dialects for what's fundamentally the same decision
("what to search for"), each source module (tools/pubmed_eutils.py,
tools/semantic_scholar_api.py) retags the same query into its own real syntax
— a deterministic, mechanical transformation of a decision already made, the
same category as the RRF math below, not a new judgment call. PubMed's is
load-bearing, not cosmetic: verified live that sending it Europe PMC's syntax
untagged lets PubMed's automatic term mapping silently drift onto the wrong
medical concept (see that module's docstring for the real example).

THE QUERY IS NEVER FORMULATED HERE. The caller — Complication Reasoning, or
Corrective Replanning when it needs evidence beyond what the complication
already carries — composes the query from live case context. Nothing in this
module maps an error category to a search term, because that mapping is exactly
the hand-authored lookup table the design forbids. This module takes a string
and returns papers.

CACHING IS PER CASE, PER SOURCE, AND KEYED BY QUERY. Two different errors in
one case frequently reason toward the same literature; re-fetching costs a
network round trip and returns identical results. The cache is keyed by the
hash of the query text, so a genuinely different question always goes out to
the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from typing import Any

from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch
from tools.europepmc_rag import search_literature
from tools.pubmed_eutils import search_pubmed
from tools.semantic_scholar_api import search_semantic_scholar
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

# Per-case, per-source, per-query cache: {(case_id, source_name, query_hash):
# [hit, ...]}. In-process is the right scope — one case is owned by one
# orchestrator task, and the cache's whole purpose is to avoid repeat fetches
# WITHIN a case.
_cache: dict[tuple[str, str, str], list[dict]] = {}

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


# One (source_name, search_fn) per real API. Each fn takes (query, k) in
# Europe PMC's own syntax and does its own retagging internally — the caller
# below never needs to know the three sources disagree on query syntax.
_SOURCES: list[tuple[str, Any]] = [
    ("europepmc", lambda q, k: search_literature(q, k=k, live=True).get("hits", [])),
    ("pubmed", search_pubmed),
    ("semantic_scholar", search_semantic_scholar),
]


async def retrieve(
    case_id: str,
    queries: list[str],
    top_n: int = DEFAULT_TOP_N,
    parent_node_id: str | None = None,
) -> tuple[list[dict], list[str], bool]:
    """Runs several short queries against three independent sources in
    parallel and merges every resulting ranked list.

    One long query does not rank badly on these APIs — it starves the ranker.
    Every term is effectively an AND clause (Europe PMC and PubMed both work
    this way once retagged; see tools/pubmed_eutils.py), so a precise-sounding
    query eliminates the papers it was meant to find. Several short queries
    along independent axes each return real candidates, and merging them is
    where the precision comes back: a paper surfaced by more than one angle,
    or by more than one source entirely, is almost certainly on topic.

    Ranking is Reciprocal Rank Fusion, which needs only each paper's POSITION
    within each result list — exactly why it's the right tool for merging
    genuinely different ranking algorithms across three unrelated APIs, none
    of which return a score comparable to the others.

    Returns (hits, node_ids, evidence_available).
    """
    wanted = [q for q in queries if q and q.strip()]
    if not wanted:
        return [], [], False

    # Genuinely concurrent across queries AND sources — these are all
    # independent network calls, and running query*source pairs sequentially
    # would add real seconds to a path that already waits on two Gemini calls.
    tasks = [
        (query, source_name, asyncio.to_thread(_search_one, case_id, query, source_name, search_fn))
        for query in wanted
        for source_name, search_fn in _SOURCES
    ]
    results = await asyncio.gather(*(t[2] for t in tasks), return_exceptions=True)

    ranked_lists: list[list[dict]] = []
    for (query, source_name, _), result in zip(tasks, results):
        if isinstance(result, Exception):
            logger.warning(
                "literature[%s]: %s query %r failed: %s", case_id, source_name, query[:60], type(result).__name__
            )
            continue
        logger.info("literature[%s]: %s: %d hit(s) for %r", case_id, source_name, len(result), query[:60])
        ranked_lists.append(result)

    merged = _reciprocal_rank_fusion(ranked_lists)[:top_n]
    if not merged:
        return [], [], False

    node_ids_written = await _write_literature_nodes(case_id, merged, parent_node_id)
    logger.info(
        "literature[%s]: %d query/source pair(s) -> %d unique papers -> top %d (%d hit by >1)",
        case_id,
        len(ranked_lists),
        sum(len(r) for r in ranked_lists),
        len(merged),
        sum(1 for h in merged if h["_query_count"] > 1),
    )
    return merged, node_ids_written, True


def _search_one(case_id: str, query: str, source_name: str, search_fn: Any) -> list[dict]:
    """One (query, source) pair, cached per case. Blocking — the caller runs
    these in threads."""
    cache_key = (case_id, source_name, query_hash(query))
    if cache_key in _cache:
        return _cache[cache_key]
    hits = search_fn(query, PER_QUERY_LIMIT)
    for hit in hits:
        hit["query_used"] = query
        hit.setdefault("source", source_name)
    _cache[cache_key] = hits
    return hits


def _paper_key(hit: dict) -> str:
    """Identity across queries AND sources. The same paper found via Europe
    PMC, PubMed, and Semantic Scholar must collapse to one entry, or the whole
    point of a second/third source — boosting a paper multiple independent
    systems agree on — is lost. DOI is the one identifier meaningfully shared
    across all three; each source's own native id (doc_id/pmcid) never
    matches another source's, so it's tried only as a fallback for whichever
    source didn't return a DOI for this particular paper."""
    return str(hit.get("doi") or hit.get("doc_id") or hit.get("pmcid") or hit.get("url") or hit.get("title", ""))


def _reciprocal_rank_fusion(ranked_lists: list[list[dict]]) -> list[dict]:
    """Merges ranked lists by summed reciprocal rank.

    A paper at position 1 of one list scores 1/(60+1); appearing in three lists
    accumulates three such terms. So a paper found by several independent
    angles — or by more than one of the three underlying APIs entirely —
    outranks one that topped a single list. That's exactly the signal we want:
    agreement between differently-phrased queries, or between unrelated
    ranking systems, is the best available evidence of topicality when no
    single comparable relevance score exists across all of them.
    """
    scores: dict[str, float] = {}
    best: dict[str, dict] = {}
    counts: dict[str, int] = {}
    queries_hit: dict[str, list[str]] = {}
    sources_hit: dict[str, set[str]] = {}

    for hits in ranked_lists:
        for rank, hit in enumerate(hits, start=1):
            key = _paper_key(hit)
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
            counts[key] = counts.get(key, 0) + 1
            queries_hit.setdefault(key, []).append(hit.get("query_used", ""))
            sources_hit.setdefault(key, set()).add(hit.get("source", "unknown"))
            best.setdefault(key, hit)

    merged = []
    for key, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        hit = dict(best[key])
        hit["_rrf_score"] = round(score, 5)
        hit["_query_count"] = counts[key]
        hit["_queries_hit"] = queries_hit[key]
        hit["_sources_hit"] = sorted(sources_hit[key])
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
                        "doi": hit.get("doi"),
                        "url": hit.get("url"),
                        "journal": hit.get("journal"),
                        "year": hit.get("year"),
                        "snippet": _clean(hit.get("snippet"))[:600],
                        # Which queries surfaced this, and how many. A paper
                        # found by several independent angles is a stronger
                        # candidate, and that is auditable rather than implied.
                        "queries_hit": hit.get("_queries_hit", []),
                        "query_count": hit.get("_query_count", 1),
                        # Which of the three real APIs actually returned this
                        # paper — a paper two independent, unrelated systems
                        # both surfaced is stronger evidence than one API's
                        # opinion alone, and that agreement is now visible
                        # rather than folded silently into one RRF number.
                        "sources_hit": hit.get("_sources_hit", [hit.get("source", "unknown")]),
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
