"""PubMed E-utilities — a second, independent literature source alongside
Europe PMC (tools/europepmc_rag.py) and Semantic Scholar
(tools/semantic_scholar_api.py). Same contract as both: takes a query the
calling agent already composed, does no query formulation of its own.

QUERY SYNTAX IS NOT THE SAME AS EUROPE PMC'S, AND GETTING THIS WRONG IS
SILENT. Verified live against the real API before writing this: PubMed's
"Automatic Term Mapping" (ATM) rewrites untagged free text, and it can drift
onto the wrong medical concept without any error — sending
"bladder neck contracture AND prostatectomy" untagged got ATM to fold in
"torticollis" (an unrelated neck-muscle condition) via a MeSH synonym match
on "neck contracture". Retested with every clause explicitly field-tagged
("bladder neck contracture"[tiab] AND prostatectomy[tiab]) and ATM produced
zero expansion — querytranslation matched the input exactly. Field-tagging a
clause is what disables ATM for that clause; this module's whole query
adapter exists to do that automatically, since the agent composes queries in
Europe PMC's syntax (see agents/complication_reasoning/subagent.py's
_QUERY_INSTRUCTION) and this module retags them for PubMed rather than
asking the agent to learn a third query dialect for a mechanical syntax
difference.

TWO REAL CALLS PER SEARCH, NOT ONE. ESearch returns PMIDs only; ESummary's
DocSum has no abstract text at all. EFetch with rettype=abstract is the real
way to get title+abstract+DOI in one follow-up call.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
import threading
from typing import Any

import requests

logger = logging.getLogger(__name__)

_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# E-utilities requires identifying the calling tool (real requirement, not
# best-practice fluff — requests without it are more likely to be throttled).
_TOOL_NAME = "surggraph"
_CONTACT_EMAIL = os.environ.get("EUTILS_CONTACT_EMAIL", "shuklaaditya473@gmail.com")
_API_KEY = os.environ.get("NCBI_API_KEY")  # optional — raises the >3 req/s ceiling if set

# NCBI's own documented ceiling without an API key is "no more than 3
# requests per second" — confirmed this is real, not just documentation
# caution: a real 4-query fanout (agents/literature_retrieval/agent.py fires
# all query/source pairs concurrently) reliably threw HTTPError on some
# requests without this. A threading.Semaphore, not an asyncio one, because
# this module's functions are sync and run inside asyncio.to_thread — a real
# OS thread pool, not the event loop, so only a thread-aware primitive
# actually serializes the underlying requests.get() calls across callers.
_RATE_LIMIT = threading.Semaphore(3 if _API_KEY else 2)

# Matches a Europe PMC-style clause: an optional FIELD: prefix, then either a
# "quoted phrase" or a single bare word. The agent's own query syntax
# (agents/complication_reasoning/subagent.py) only ever produces these two
# shapes, space- or AND-joined.
_CLAUSE_RE = re.compile(r'(?:(?P<field>TITLE|ABSTRACT|KW|MESH):)?(?:"(?P<phrase>[^"]+)"|(?P<word>\S+))')

# Europe PMC field prefixes have no exact PubMed equivalent, so this maps to
# the closest real PubMed field tag rather than inventing one:
#   TITLE   -> [ti]     real PubMed title-only tag
#   ABSTRACT-> [tiab]   PubMed has no abstract-only tag; [tiab] is the closest
#   KW      -> [tiab]   PubMed's own keyword tag ([ot]) has spotty coverage;
#                        [tiab] is the pragmatic equivalent, not a perfect one
#   MESH    -> [mesh]   real PubMed MeSH tag, direct equivalent
#   (none)  -> [tiab]   bare terms default to title/abstract, never left
#                        untagged (that's exactly what triggers ATM drift)
_FIELD_TO_TAG = {"TITLE": "ti", "ABSTRACT": "tiab", "KW": "tiab", "MESH": "mesh", None: "tiab"}


def _to_pubmed_query(europepmc_style_query: str) -> str:
    """Retags a Europe-PMC-syntax query for PubMed, preserving AND-of-clauses
    semantics while forcing every clause through an explicit field tag so
    PubMed's automatic term mapping never gets a chance to run on it."""
    clauses = []
    for m in _CLAUSE_RE.finditer(europepmc_style_query):
        term = m.group("phrase") or m.group("word")
        if not term or term.upper() == "AND":
            continue
        tag = _FIELD_TO_TAG.get(m.group("field"))
        quoted = f'"{term}"' if " " in term else term
        clauses.append(f"{quoted}[{tag}]")
    return " AND ".join(clauses)


def _eutils_params(**extra: Any) -> dict[str, Any]:
    params = {"tool": _TOOL_NAME, "email": _CONTACT_EMAIL, **extra}
    if _API_KEY:
        params["api_key"] = _API_KEY
    return params


def _esearch(query: str, k: int) -> list[str]:
    with _RATE_LIMIT:
        resp = requests.get(
            _ESEARCH_URL,
            params=_eutils_params(db="pubmed", term=query, retmax=k, retmode="json"),
            timeout=15,
        )
    resp.raise_for_status()
    return resp.json().get("esearchresult", {}).get("idlist", [])


def _text(article: ET.Element, path: str) -> str | None:
    el = article.find(path)
    return el.text if el is not None and el.text else None


def _efetch_abstracts(pmids: list[str]) -> list[dict[str, Any]]:
    if not pmids:
        return []
    with _RATE_LIMIT:
        resp = requests.get(
            _EFETCH_URL,
            params=_eutils_params(db="pubmed", id=",".join(pmids), rettype="abstract", retmode="xml"),
            timeout=20,
        )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    hits = []
    for article in root.iterfind(".//PubmedArticle"):
        pmid = _text(article, ".//PMID")
        title = _text(article, ".//ArticleTitle")
        # Abstract can carry multiple labeled AbstractText sections
        # (Background/Methods/Results/...) — join them into one snippet
        # rather than keeping only the first, which is often just "Background".
        abstract_parts = [el.text for el in article.iterfind(".//Abstract/AbstractText") if el.text]
        abstract = " ".join(abstract_parts)
        if not title or not abstract:
            continue
        doi = None
        for eloc in article.iterfind(".//ELocationID"):
            if eloc.get("EIdType") == "doi":
                doi = eloc.text
        year = _text(article, ".//JournalIssue/PubDate/Year")
        journal = _text(article, ".//Journal/Title")
        hits.append(
            {
                "doc_id": f"pubmed:{pmid}",
                "pmcid": None,
                "doi": doi,
                "title": title,
                "snippet": abstract[:500],
                "score": None,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
                "year": int(year) if year and year.isdigit() else None,
                "journal": journal,
                "source": "pubmed",
            }
        )
    return hits


def search_pubmed(query: str, k: int = 10) -> list[dict[str, Any]]:
    """Searches PubMed live via E-utilities. `query` is expected in the same
    Europe-PMC-flavored syntax the calling agent already produces — this
    function retags it for PubMed itself; the caller does not need to know
    the two APIs disagree on syntax."""
    pubmed_query = _to_pubmed_query(query)
    if not pubmed_query:
        return []
    pmids = _esearch(pubmed_query, k)
    return _efetch_abstracts(pmids)
