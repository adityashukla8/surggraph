"""search_literature — the retrieval tool shared by the Complication
Enumeration Agent and the reactive Literature Agent.

This module does no phase-to-complication mapping. The CALLER (Gemini, via
tool-calling) formulates `query` from live case context; this function only
does semantic search over the pre-cached Europe PMC corpus built by
scripts/precache_rag.py, or a live Europe PMC call when `live=True`. Query
strategy is entirely the agent's choice — that's the actual
least-hardcoding property (see that script's module docstring and plan §3).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import faiss
import numpy as np
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "rag_cache"
CORPUS_PATH = DATA_DIR / "corpus.jsonl"
EMBEDDINGS_PATH = DATA_DIR / "embeddings.npy"
EUROPEPMC_BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

_EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"


class RagCorpusUnavailable(RuntimeError):
    """Raised when the pre-cached corpus hasn't been built yet — fails
    closed rather than silently returning no results, so a caller can tell
    the difference between 'no matches' and 'corpus missing'."""


@lru_cache(maxsize=1)
def _load_corpus() -> list[dict[str, Any]]:
    if not CORPUS_PATH.exists():
        raise RagCorpusUnavailable(f"{CORPUS_PATH} not found — run scripts/precache_rag.py first")
    with open(CORPUS_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


@lru_cache(maxsize=1)
def _load_index() -> faiss.Index:
    if not EMBEDDINGS_PATH.exists():
        raise RagCorpusUnavailable(f"{EMBEDDINGS_PATH} not found — run scripts/precache_rag.py first")
    embeddings = np.load(EMBEDDINGS_PATH).astype(np.float32)
    faiss.normalize_L2(embeddings)  # cosine similarity via inner product on normalized vectors
    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)
    return index


@lru_cache(maxsize=1)
def _embedder():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(_EMBEDDING_MODEL_NAME)


def _search_cached(query: str, k: int) -> list[dict[str, Any]]:
    corpus = _load_corpus()
    index = _load_index()
    query_vec = _embedder().encode([query], convert_to_numpy=True).astype(np.float32)
    faiss.normalize_L2(query_vec)
    scores, indices = index.search(query_vec, min(k, len(corpus)))

    hits = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0:
            continue
        doc = corpus[idx]
        hits.append(
            {
                "doc_id": doc["doc_id"],
                "pmcid": doc.get("pmcid"),
                "title": doc["title"],
                "snippet": doc["abstract"][:500],
                "score": float(score),
                "url": doc.get("url"),
                "year": doc.get("year"),
                "journal": doc.get("journal"),
            }
        )
    return hits


def _search_live(query: str, k: int) -> list[dict[str, Any]]:
    """One 'fresh evidence' path that bypasses the cache and hits Europe PMC
    directly — used sparingly (default off) so the demo doesn't depend on
    live network access for reliability."""
    params = {
        "query": f"({query}) AND OPEN_ACCESS:y AND SRC:MED",
        "format": "json",
        "resultType": "core",
        "pageSize": k,
    }
    resp = requests.get(EUROPEPMC_BASE_URL, params=params, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("resultList", {}).get("result", [])
    hits = []
    for r in results:
        if not r.get("abstractText") or not r.get("title"):
            continue
        hits.append(
            {
                "doc_id": r.get("id"),
                "pmcid": r.get("pmcid"),
                "doi": r.get("doi"),
                "title": r["title"],
                "snippet": r["abstractText"][:500],
                "score": None,  # live search has no embedding-similarity score
                "url": f"https://europepmc.org/article/MED/{r.get('id')}" if r.get("id") else None,
                "year": r.get("pubYear"),
                "journal": r.get("journalInfo", {}).get("journal", {}).get("title"),
                "source": "europepmc",
            }
        )
    return hits


def search_literature(query: str, k: int = 5, live: bool = False) -> dict[str, Any]:
    """Searches the pre-cached open-access corpus (default) or Europe PMC
    live (`live=True`). `query` must be formulated by the calling agent from
    live case context — this function performs no phase-to-complication
    mapping of its own."""
    hits = _search_live(query, k) if live else _search_cached(query, k)
    corpus_size = None if live else len(_load_corpus())
    return {"hits": hits, "corpus_size": corpus_size, "queried_live": live}
