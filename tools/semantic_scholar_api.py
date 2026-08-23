"""Semantic Scholar Graph API — a third, independent literature source
alongside Europe PMC (tools/europepmc_rag.py) and PubMed
(tools/pubmed_eutils.py). Same contract as both: takes a query the calling
agent already composed, does no query formulation of its own.

DIFFERENT QUERY SEMANTICS FROM THE OTHER TWO. Europe PMC and PubMed are
boolean field-search systems — every clause is a constraint the result must
literally satisfy. Semantic Scholar's /paper/search is a trained relevance
ranker over free-text keywords, not a boolean engine — there is no AND/OR,
no field-scoping syntax to speak of. Sending it the other sources' bare
"bladder neck contracture" "prostatectomy"-style AND-of-quoted-phrases would
be sending syntax markers (the quotes, the field prefixes) it has no reason
to understand; this module strips that down to the underlying keywords
before searching.

RATE LIMIT IS REAL AND ALREADY HIT IN TESTING, NOT A HYPOTHETICAL. Confirmed
live: the unauthenticated tier returned 429 on the very first call made
during development, and stayed 429 after a 15s wait — this is not a burst
limit that clears quickly, at least not reliably. An API key
(SEMANTIC_SCHOLAR_API_KEY) raises this a lot but requires applying for one
(https://www.semanticscholar.org/product/api#api-key-form). Deliberately not
required: this source runs unauthenticated by default and is expected to
fail with 429 fairly often until/unless a key is added — treated exactly
like any other retrieval failure (agents/literature_retrieval/agent.py's
asyncio.gather(..., return_exceptions=True) already tolerates one source
failing without affecting the others), not a reason to block the feature.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")

# Strips Europe-PMC-style field prefixes (TITLE:/ABSTRACT:/KW:/MESH:) and
# quote marks — syntax the other two sources' boolean engines need, that a
# free-text relevance ranker has no use for and could otherwise confuse.
_FIELD_PREFIX_RE = re.compile(r"\b(?:TITLE|ABSTRACT|KW|MESH):", re.IGNORECASE)


def _to_semantic_scholar_query(europepmc_style_query: str) -> str:
    without_prefixes = _FIELD_PREFIX_RE.sub("", europepmc_style_query)
    without_quotes = without_prefixes.replace('"', "")
    # "AND" is a real boolean operator in the source syntax; here it's just
    # another word diluting the keyword query, so it's dropped like any
    # other non-content token rather than kept as literal text.
    words = [w for w in without_quotes.split() if w.upper() != "AND"]
    return " ".join(words)


def search_semantic_scholar(query: str, k: int = 10) -> list[dict[str, Any]]:
    """Searches Semantic Scholar live via the Graph API's relevance search.
    `query` is expected in the same Europe-PMC-flavored syntax the calling
    agent already produces — this function converts it to plain keywords."""
    keyword_query = _to_semantic_scholar_query(query)
    if not keyword_query:
        return []

    headers = {"x-api-key": _API_KEY} if _API_KEY else {}
    params = {
        "query": keyword_query,
        "fields": "title,abstract,year,venue,externalIds",
        "limit": k,
    }
    try:
        resp = requests.get(_SEARCH_URL, params=params, headers=headers, timeout=15)
        if resp.status_code == 429:
            logger.warning("semantic_scholar: rate-limited (429) for %r — treating as no results", keyword_query[:60])
            return []
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("semantic_scholar: request failed for %r: %s", keyword_query[:60], exc)
        return []

    papers = resp.json().get("data", [])
    hits = []
    for paper in papers:
        title = paper.get("title")
        abstract = paper.get("abstract")
        if not title or not abstract:
            continue
        external_ids = paper.get("externalIds") or {}
        doi = external_ids.get("DOI")
        paper_id = paper.get("paperId")
        hits.append(
            {
                "doc_id": f"semanticscholar:{paper_id}" if paper_id else None,
                "pmcid": None,
                "doi": doi,
                "title": title,
                "snippet": abstract[:500],
                "score": None,
                "url": f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else None,
                "year": paper.get("year"),
                "journal": paper.get("venue"),
                "source": "semantic_scholar",
            }
        )
    return hits
