"""plan_v2 §16.9 Step 3 — tools/feedback_kb.py's read path.

Offline structural tests (caching, bounding, empty-block byte-identity)
plus real live retrieval against the real memories scripts/test_feedback_
write.py already wrote — same convention as the rest of this feature: no
mocks for the actual GEAP call.
"""

from __future__ import annotations

import asyncio

import pytest

import tools.feedback_kb as feedback_kb


# --- Offline / structural ----------------------------------------------------


def test_empty_scope_is_falsy_and_bounded_constants_are_sane():
    assert feedback_kb.MAX_DIRECTIVES > 0
    assert feedback_kb.MAX_OBSERVATIONS > 0
    assert feedback_kb.MAX_ITEM_CHARS > 0


def test_truncate_respects_max_item_chars():
    long_text = "x" * (feedback_kb.MAX_ITEM_CHARS + 100)
    truncated = feedback_kb._truncate(long_text)
    assert len(truncated) == feedback_kb.MAX_ITEM_CHARS
    assert truncated.endswith("…")

    short_text = "short"
    assert feedback_kb._truncate(short_text) == short_text


@pytest.mark.asyncio
async def test_feedback_block_empty_scope_returns_empty_string(monkeypatch):
    # No real network call — patch the module-level memory_bank functions
    # this ONE test relies on, to prove the pure "nothing found -> no block"
    # branch without needing a genuinely-empty live scope (every other test
    # in this file uses the real, populated one).
    monkeypatch.setattr(feedback_kb.memory_bank, "retrieve_memories", lambda *a, **k: [])
    feedback_kb._cache.clear()
    block = await feedback_kb.feedback_block("corrective_replanning", "some query with no matches", "fake-engine")
    assert block == ""


@pytest.mark.asyncio
async def test_feedback_block_fails_soft_on_exception(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("simulated GEAP outage")

    monkeypatch.setattr(feedback_kb.memory_bank, "retrieve_memories", _raise)
    feedback_kb._cache.clear()
    # Must not raise — this is the property that keeps a Memory Bank outage
    # from ever breaking a real case run.
    block = await feedback_kb.feedback_block("divergence_detection", "anything", "fake-engine")
    assert block == ""


def test_cached_retrieve_hits_cache_on_second_call(monkeypatch):
    calls = []

    def fake_retrieve(scope, agent_engine, query, top_k):
        calls.append((scope, agent_engine, query, top_k))
        return ["one fact"]

    monkeypatch.setattr(feedback_kb.memory_bank, "retrieve_memories", fake_retrieve)
    feedback_kb._cache.clear()

    scope = feedback_kb.directive_scope("divergence_detection")
    r1 = feedback_kb._cached_retrieve(scope, "engine-x", None, 5)
    r2 = feedback_kb._cached_retrieve(scope, "engine-x", None, 5)
    assert r1 == r2 == ["one fact"]
    assert len(calls) == 1, "second call within the TTL must hit the cache, not the network"


def test_cached_retrieve_misses_on_different_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(feedback_kb.memory_bank, "retrieve_memories", lambda *a, **k: calls.append(a) or ["fact"])
    feedback_kb._cache.clear()

    feedback_kb._cached_retrieve(feedback_kb.directive_scope("divergence_detection"), "engine-x", None, 5)
    feedback_kb._cached_retrieve(feedback_kb.directive_scope("literature_retrieval"), "engine-x", None, 5)
    assert len(calls) == 2, "a genuinely different scope must not reuse another scope's cache entry"


# --- Live, against the real memories scripts/test_feedback_write.py wrote --


@pytest.mark.asyncio
async def test_feedback_block_live_divergence_detection_carries_the_real_observation():
    from agents.surgbot.feedback import _synthesis_resource_name  # local import: only these live tests need it

    engine_name = await asyncio.to_thread(_synthesis_resource_name)
    feedback_kb._cache.clear()
    block = await feedback_kb.feedback_block(
        "divergence_detection", "first divergence corrective plan false positive", engine_name
    )
    print(f"\nREAL rendered block for divergence_detection:\n{block}")
    assert "REVIEWER FEEDBACK" in block
    assert "NOT ground truth" in block
    assert "false positive" in block


@pytest.mark.asyncio
async def test_feedback_block_live_literature_retrieval_carries_the_real_directive():
    from agents.surgbot.feedback import _synthesis_resource_name

    engine_name = await asyncio.to_thread(_synthesis_resource_name)
    feedback_kb._cache.clear()
    block = await feedback_kb.feedback_block("literature_retrieval", "unrelated query about suturing", engine_name)
    print(f"\nREAL rendered block for literature_retrieval:\n{block}")
    assert "Standing guidance" in block
    assert "10 years" in block


@pytest.mark.asyncio
async def test_feedback_block_live_corrective_replanning_carries_the_real_observation():
    # corrective_replanning was seeded with real feedback during this
    # feature's own E2E verification (plan_v2 §16.9 Step 5) — updated from
    # an earlier "must be empty" assertion to reflect that real state,
    # rather than asserting something no longer true.
    from agents.surgbot.feedback import _synthesis_resource_name

    engine_name = await asyncio.to_thread(_synthesis_resource_name)
    feedback_kb._cache.clear()
    block = await feedback_kb.feedback_block("corrective_replanning", "needle handling error severity", engine_name)
    print(f"\nREAL rendered block for corrective_replanning:\n{block}")
    assert "REVIEWER FEEDBACK" in block
    assert "too conservative" in block


@pytest.mark.asyncio
async def test_feedback_block_live_error_detection_is_empty_no_feedback_ever_routed_there():
    # error_detection is captured-but-not-consumed in v1 (plan_v2 §16.7) —
    # this repo's tests never write error-anchored feedback, so this scope
    # is the genuine "nothing here" case: empty-block byte-identity,
    # verified live rather than assumed.
    from agents.surgbot.feedback import _synthesis_resource_name

    engine_name = await asyncio.to_thread(_synthesis_resource_name)
    feedback_kb._cache.clear()
    block = await feedback_kb.feedback_block("error_detection", "any query", engine_name)
    print(f"\nREAL rendered block for error_detection (expected empty): {block!r}")
    assert block == "", "no feedback has ever been routed to error_detection — must be byte-empty, not a placeholder"
