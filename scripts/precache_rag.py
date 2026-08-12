"""Pre-caches an open-access literature corpus from Europe PMC for the
Complication Enumeration / Literature Agent to retrieve from at runtime.

IMPORTANT — what this script is and is NOT:
This script's SEED_QUERIES exist only to decide what's worth fetching and
caching offline for demo reliability (per plan §3.1). They are NOT a
phase-to-complication lookup table and are never consulted at runtime — the
agent formulates its own retrieval query from live case context (current
phase, entities, patient risk flags) and searches the resulting corpus via
tools/europepmc_rag.py::search_literature(). Swapping, adding, or removing
seed queries here only changes what's available to retrieve; it never
changes what the agent is allowed to conclude.

EAU/AUA/NICE/Cochrane guideline text must never be added to SEED_QUERIES or
ingested into the corpus — their terms prohibit LLM ingestion. Only
peer-reviewed, open-access (OPEN_ACCESS:y) PMC articles are fetched here.

Usage: uv run scripts/precache_rag.py
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

EUROPEPMC_BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "rag_cache"
CORPUS_PATH = DATA_DIR / "corpus.jsonl"
EMBEDDINGS_PATH = DATA_DIR / "embeddings.npy"
RESULTS_PER_QUERY = 6
REQUEST_DELAY_S = 0.15  # stay well under Europe PMC's ~10 req/s limit

# Seed queries only — see module docstring. Sourced from the RARP
# complication literature already cited in project research (Novara et al.
# Eur Urol 2012 PMID 22749853; Tewari et al. Eur Urol 2012 PMID 22405509),
# covering the complication classes clustered across the RARP procedure.
SEED_QUERIES = [
    "robot-assisted radical prostatectomy complications",
    "dorsal venous complex bleeding prostatectomy",
    "neurovascular bundle injury radical prostatectomy",
    "positive surgical margin radical prostatectomy",
    "urethrovesical anastomosis leak prostatectomy",
    "rectal injury radical prostatectomy",
    "pelvic lymph node dissection complications prostatectomy",
    "obturator nerve injury prostatectomy",
    "lymphocele robotic prostatectomy",
    "trocar injury laparoscopic pneumoperitoneum complications",
]


def fetch_query(query: str) -> list[dict]:
    params = {
        "query": f"({query}) AND OPEN_ACCESS:y AND SRC:MED",
        "format": "json",
        "resultType": "core",
        "pageSize": RESULTS_PER_QUERY,
    }
    resp = requests.get(EUROPEPMC_BASE_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("resultList", {}).get("result", [])


def to_corpus_entry(result: dict, source_query: str) -> dict | None:
    abstract = result.get("abstractText")
    title = result.get("title")
    if not abstract or not title:
        return None  # skip entries with no abstract to embed — nothing to retrieve against
    pmcid = result.get("pmcid")
    return {
        "doc_id": result.get("id") or pmcid or result.get("doi"),
        "pmcid": pmcid,
        "title": title,
        "abstract": abstract,
        "journal": result.get("journalInfo", {}).get("journal", {}).get("title"),
        "year": result.get("pubYear"),
        "doi": result.get("doi"),
        "url": f"https://europepmc.org/article/MED/{result.get('id')}" if result.get("id") else None,
        "source_query": source_query,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    entries_by_doc_id: dict[str, dict] = {}
    for query in SEED_QUERIES:
        print(f"Querying Europe PMC: {query!r}")
        try:
            results = fetch_query(query)
        except requests.RequestException as e:
            print(f"  [SKIP] request failed: {e}")
            continue
        for result in results:
            entry = to_corpus_entry(result, query)
            if entry is None or not entry["doc_id"]:
                continue
            if entry["doc_id"] not in entries_by_doc_id:
                entries_by_doc_id[entry["doc_id"]] = entry
        print(f"  -> {len(results)} results, {len(entries_by_doc_id)} unique docs so far")
        time.sleep(REQUEST_DELAY_S)

    entries = list(entries_by_doc_id.values())
    if not entries:
        print("\n[FAILED] No corpus entries fetched — nothing written. Check network access to Europe PMC.")
        return 1

    with open(CORPUS_PATH, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    print(f"\nWrote {len(entries)} entries to {CORPUS_PATH}")

    print("Embedding corpus with all-MiniLM-L6-v2 (CPU)...")
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer("all-MiniLM-L6-v2")
    texts = [f"{e['title']}\n\n{e['abstract']}" for e in entries]
    embeddings = model.encode(texts, show_progress_bar=True, convert_to_numpy=True)
    np.save(EMBEDDINGS_PATH, embeddings.astype(np.float32))
    print(f"Wrote {embeddings.shape} embeddings to {EMBEDDINGS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
