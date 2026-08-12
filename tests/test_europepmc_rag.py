"""Tests for the retrieval tool itself (tools/europepmc_rag.py). The full
test_complication_enumeration.py from the plan (trajectory-patch integration,
Verifier rejection of empty evidence, confirmation-signal flips) is written
once the Complication Enumeration Agent exists — this covers the retrieval
primitive it and the reactive Literature Agent both call.
"""

from __future__ import annotations

from tools.europepmc_rag import _load_corpus, search_literature


def test_search_returns_only_real_corpus_docs():
    corpus_ids = {entry["doc_id"] for entry in _load_corpus()}
    result = search_literature("neurovascular bundle injury during prostatectomy", k=5)
    assert result["hits"], "expected at least one hit for a query matching the seeded corpus topics"
    for hit in result["hits"]:
        assert hit["doc_id"] in corpus_ids


def test_search_respects_k():
    result = search_literature("robotic prostatectomy complications", k=2)
    assert len(result["hits"]) <= 2


def test_search_reports_corpus_size():
    result = search_literature("urethrovesical anastomosis leak", k=3)
    assert result["corpus_size"] == len(_load_corpus())
    assert result["queried_live"] is False


def test_hits_carry_citation_fields():
    result = search_literature("lymphocele after pelvic lymph node dissection", k=3)
    for hit in result["hits"]:
        assert hit["title"]
        assert hit["snippet"]
        assert hit["doc_id"]
