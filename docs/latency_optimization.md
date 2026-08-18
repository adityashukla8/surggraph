# Latency optimization — living doc

**Started:** 2026-08-14. **Status:** In progress — updated as each change lands, not written once and left stale.

Tracks the real, measured latency problem, what was explored, what was decided, and what actually changed — kept separate from the architecture plan file so it survives context reloads on its own and stays scannable as a single before/after record.

## Why this exists

Real per-window Gemini latency (confirmed this session, see "Before" below) is far slower than the video's own real-time playback. Given Anticipation Agent's whole job is predicting *ahead*, a perception layer that lags by minutes has nothing current to anticipate from. Goal: reduce latency and cover the *entire* video, explicitly trading cost and some accuracy to get there — not trying to preserve the original CARES-full-fidelity cost profile.

## Before — the state that prompted this

Measured directly from real runs this session, not estimated:

| Measurement | Value | Source |
|---|---|---|
| Native video call, single 10s window | 56.7s | `docs/monitor_agent_video_input_benchmark.md` |
| Still-frame call, same window | 19.8s | same benchmark — real ~2.9x gap |
| Prompt tokens, native video | 4,562 (66 tokens/frame-equivalent) | same benchmark |
| Prompt tokens, still frames | 12,423 (1,100 tokens/frame — Gemini's image tiling) | same benchmark |
| Monitor's real per-window latency, offline sweep | median 163s, mean 165s (259 real windows, one network-outage outlier excluded) | `data/validation/monitor_accuracy.jsonl` timestamps, `max_concurrent_windows=2` |
| One real live Orchestrator run, this session | Scene Graph Builder done ~61-70s (1 call); Monitor done ~178-183s (2 sequential rounds of 3 parallel calls — this window escalated) | real snapshot timestamps, case `case-83732ab25ee9` |
| Real video duration | 271.57s (4:31) | `find_video_duration_s('video_01')` |
| Confirmed quota ceiling | Real 429s (Vertex AI Dynamic Shared Quota) observed at just 3-4 concurrent calls, earlier this session | led to the existing `HttpRetryOptions`(attempts=8, max_delay=90s) + concurrency semaphores |

**Root causes, in order of impact:**
1. Monitor's 2-pass structure is *sequential* (deep pass waits for screen pass's escalation decision) — roughly doubles wall time whenever something escalates.
2. Native video's per-call cost (~56.7s) is dominated by GCS fetch + server-side decode, not something tunable via prompt changes.
3. Only one bounded ~10s window was ever analyzed live — the other ~261 real seconds of the video had zero live coverage regardless of speed.

## Explored — real options researched, with verdicts

Full write-up and sources in the conversation; verdicts recorded here:

| Option | Verdict | Why |
|---|---|---|
| Gemini Live API | **Rejected** | No model at version 3.5+ supports it (only `gemini-3.1-flash-live-preview`/2.5-family) — hard hackathon eligibility conflict, confirmed directly. Also caps visual input at ≤1 FPS stills, a real regression from native video. |
| Cascade: cheap/fast tier + expensive/accurate tier | **Adopted** | Confirmed as the real 2026 production pattern (multiple sources). Already Monitor's rough shape; the fix is making the frequent tier genuinely cheap (still frames) and running both tiers concurrently instead of sequentially. |
| Gemini implicit context caching | **Adopted where free** | Automatic for Gemini 2.5+ models, no code change, reduces cost+latency for repeated identical prefixes. Plan: log `usage_metadata.cached_content_token_count` on real calls to confirm it's firing, not just assume. |
| Speculative/unconditional parallel deep pass (all 6 categories, not just escalated) | **Adopted** | Trades cost (up to 18 calls/window instead of ≤6) for wall-time (`max(screen, deep)` instead of `screen + deep`), and gets a real verdict on all 6 categories every window, not just the escalated one. |
| Dead reckoning (Anticipation predicts ahead of the slower perception agents) | **Adopted** | Real, precedented pattern (visual-inertial odometry, networked-game latency hiding). The project's own schema already has the exact mechanism (`TrajectoryPatch.confirmation_signal: pending→confirmed/refuted`) — Anticipation Agent just needs to exist and use it. |
| Feeding real error labels to Monitor as input context | **Rejected** | Different, more severe case than phase/mask context — Monitor's entire value proposition *is* detecting errors; handing it the answer leaves zero real autonomous work, directly undercuts the hackathon's 40%-weighted Innovation/autonomous-action criterion. |
| Apache Beam | **Rejected** | Checked directly: Beam's LLM/RunInference latency story is for *self-hosted* models on Beam workers ("reasoning happening in-stream"), not calling an external managed API. For calling Vertex AI Gemini via REST, Beam wraps the same external HTTP call `asyncio` already makes, adds a real runner/DAG/worker-provisioning layer, and touches none of the actual bottleneck (Gemini's own serving latency + Vertex AI quota queuing). |
| True real-time (video's own 271s ≈ analysis wall-clock) | **Rejected as a target** | Full-video coverage at real-time pace needs ~9-10 windows in flight at once, ~150-190 concurrent real Gemini calls at peak — far past the confirmed real quota ceiling (429s at just 3-4 concurrent). Not a client-side-tunable problem. Honest target: full video coverage in minutes, not real-time, degrading gracefully via existing retry logic under load. |
| Anticipation fed the real ground-truth phase ID directly as input | **Rejected** | Caught mid-build: the agent never watched any video, its whole "task" reduced to receiving the answer and looking up a probability table — same category of flaw as Monitor's original SEDMamba-lookup design. Real published surgical-anticipation work (Rivoir et al. arXiv:2007.00548, SWAG arXiv:2412.18849, SuPRA arXiv:2403.06200) never treats current phase as a given/oracle input; SurgRAW (arXiv:2503.10265) is the direct zero-shot multi-agent VLM precedent, same role CARES played for Monitor. See plan §13. |
| Exemplar reference frames + numeral output for Anticipation's self-perception | **Rejected** | Fixed the input side (genuine live visual reasoning) but not the output: the answer was still an opaque numeral, which would've shown as "Phase 1" on the UI — fails the actual goal of demonstrating real LLM surgical reasoning on screen. Corrected design: free-form semantic naming only, numeral demoted to an internal, never-shown bookkeeping key (plan §13.3). |

## Current — what's actually being built now (Aug 14)

1. **Monitor Agent**: screen pass → still frames; deep pass → **also still frames as of the second pass below** (was native video, unconditional across all 6 categories, fixed "attending" tier, drops Ψ-routing — the plan's own cut-order already flagged that as ~3% accuracy impact). Screen and deep run concurrently, not sequentially. Phase-node writes are now create-if-missing only — never clobbers a real semantic label (Scene Graph Builder's/Anticipation's own) with Monitor's own generic `"Phase {id}"` fallback (plan §13.4).
2. **Scene Graph Builder**: real segmentation-mask image context removed (real, confirmed-expensive per-image tiling cost); real phase-ID text context kept (negligible cost, real signal); **native video switched to still frames as of the second pass below**. Phase-node label now uses the window's own real, live `activity_description` instead of the generic ID-based label (plan §13.4).
3. **Orchestrator**: sweeps the *entire* real video duration, not one bounded window; concurrency semaphores raised (exact values below, once tuned against real quota behavior).
4. **Anticipation Agent**: built, twice-revised (plan §13). Final design: Gemini never sees or produces a numeric phase ID — it only sees real current-window stills and an anonymized statistical-confidence hint (the real transition prior's *shape*, no category label), and answers entirely in its own words what phase this looks like and what's next. The real numeric ID stays only as an internal graph-node bookkeeping key computed by the wrapper, never told to Gemini or shown on the UI. Dead-reckons ahead of Monitor/Scene Graph Builder's slower real observations; reconciliation redirects a predicted edge onto the real node an independent agent later corroborates (slug-based text matching, not semantic equivalence — a disclosed limitation).
5. **Every graph node gets a real video-time range** (not just phase/event nodes as before) — a user-requested consistency fix alongside this work, not a latency change itself.

## After — real measurements, bounded end-to-end run (Aug 14)

Real run via `agents/orchestrator/agent.py::open_case`, all three agents concurrent (Monitor + Scene Graph Builder + Anticipation), bounded to `video_01`'s real [0.0, 45.0)s (5 non-overlapping 10s windows; full-video timing not yet run — this bound was chosen to keep interactive-verification cost/time reasonable, same code path scales to the full 271.57s video with more windows):

| Measurement | Value |
|---|---|
| Real wall time (open_case) | 375.0s for the 45s window (~115 real Gemini calls total: Monitor ~21/window × 5, Scene Graph Builder 1/window × 5, Anticipation 1/window × 5) |
| Real divergences fired | 4 (`out_of_view`@20s, `multiple_attempts`/`needle_handling`/`instrument_control`@30s — all real composite scores/confidences, no fabricated data) |
| Real graph size | 29 nodes, 39 edges, seq=105 (real Firestore-backed patch count) |
| Phase-node labels | 100% real semantic text (e.g. `phase:0`: "A bipolar forceps briefly retracts pelvic tissue on the left side, exposing the prostate..."; `phase:3`: "The surgeon is performing urethrovesical anastomosis...") — zero numerals leaked to any label, confirming the plan §13.3/§13.4 fix |
| Anticipation convergence | 1/5 real forecasts independently confirmed within the 120s reconciliation window (window 0's forecast — made from real frames 0-10s, before Monitor/Scene Graph Builder had reached that point — correctly predicted "urethrovesical anastomosis," later matched against Scene Graph Builder's own real `phase:3` label and had its edge redirected to point at that real node); 4/5 honestly left `pending` (not forced to confirmed/refuted) |
| Anonymized-hint leakage | Zero — confirmed via `test_summarize_transition_confidence_never_leaks_category_labels` |

**One real infra fix along the way:** `tools/state_tools.py`'s HTTP client timeout (10s) was too tight once three agents write concurrently — real Firestore transaction contention (all writes to one case_id serialize through that case's own `seq` document) legitimately took longer than 10s under this load in one real run, producing a client-side `ReadTimeout` even though the server would have succeeded given more time. Raised to 30s; confirmed idle-latency stayed sub-100ms in the same session (not a stuck server).

Full-video timing and the offline `anticipation_accuracy.jsonl`/`monitor_accuracy.jsonl` accuracy summaries are still pending — this section will be updated again once those run.

## Second pass (Aug 14, same day) — real live-use finding: ~2 minutes before the graph looked meaningfully populated

Playing the video and watching the live graph (not a synthetic benchmark) surfaced a real UX problem the first pass's numbers didn't capture: nothing meaningful appeared for roughly the first 2 minutes of real video playback. Root cause, once traced: making Monitor's deep tier *unconditional* (first pass) meant every window now pays native video's real per-call cost — and that cost is dominated by GCS fetch + server-side decode, not clip length (confirmed in the original benchmark). So a shorter window alone wouldn't have fixed it; the fix has to be the input modality.

| Option | Verdict | Why |
|---|---|---|
| Shrink window_s alone (10s → 5s), keep native video | **Rejected as insufficient on its own** | Native video's latency isn't proportional to clip length — the dominant cost is fetch/decode overhead per call, not frames processed. Halving the window doesn't meaningfully cut per-call time; it would just double the number of full-cost calls needed to cover the same video. |
| Switch Monitor's deep tier (and Scene Graph Builder) from native video to still frames | **Adopted** | The real, already-confirmed lever (~2.9x per-call speedup, same benchmark as the first pass). Once every window's deep pass is stills, a shorter window's benefit is real (frame count scales with content, so 5s windows genuinely need fewer/smaller frame samples than 10s ones) — this is why window-shrinking and frame-driven input are adopted together, not independently. |
| Give the deep tier a denser/separate still-frame profile than the screen tier (preserve more of the screen/deep distinction) | **Rejected for now** | Simpler to reuse the exact same `STILL_FRAME_PROFILE` for both tiers — the cascade's remaining real distinction is REASONING framing (broad 6-category scan vs. focused single-category, tier-voiced re-examination of the same frames), not input richness. Revisit if accuracy data says otherwise. |

**Disclosed accuracy tradeoff, carried forward from the first pass, now wider in scope:** native video previously caught a real event still-frame sampling missed (the original reason Monitor migrated to it). That advantage is now gone from BOTH Monitor's deep tier and Scene Graph Builder, not just avoided for the screen tier — accepted per the user's explicit, repeated latency-over-accuracy priority, not a silent regression.

**What changed, concretely:**
- `agents/monitor/subagents.py`: `VIDEO_FPS_PROFILE` removed (no longer used anywhere); `SCREEN_STILL_FRAME_PROFILE` renamed to `STILL_FRAME_PROFILE` (now shared by both tiers) with frame counts roughly halved (temporal 10→5, procedural 6→3, spatial unchanged at 4) to keep sampling *density* constant now that windows are 5s, not 10s.
- `agents/monitor/coordinator.py`: deep tier (`_run_deep_pass_all_categories`) now samples local still frames via the same mechanism as the screen tier, instead of fetching native video from GCS. `run_monitor_window` no longer needs a GCS URI/mime type at all.
- `agents/scene_graph_builder/agent.py`: same switch — `run_scene_graph_window` now samples 5 local still frames (native resolution, no resize — entity-naming benefits from detail more than Monitor's motion-sensitive temporal role does) instead of a native video clip.
- Window size (`window_s`) default dropped from 10.0 to 5.0 across Monitor, Scene Graph Builder, and Anticipation, keeping all three on the same real-time cadence.
- `tools/video_utils.py::build_video_window_content` is left in place (a complete, working, generic utility, not agent-specific glue) even though nothing currently calls it — a future need for native video can reuse it without rebuilding it.

Real before/after timing for this second pass is not yet measured — pending a live run.

**Real accuracy check, run 2026-08-15** (`uv run scripts/run_monitor_validation_sweep.py video_01 --stride-s 5` — 53 windows, coarser than the original 262-window/1s-stride sweep, so directional not exact, but the same real pipeline code): **macro_f1 = 0.515** (vs. the pre-restructuring baseline of 0.408, archived at `data/validation/monitor_accuracy_2026-08-13_pre_framedriven.jsonl`; CARES reports 0.543 on this dataset). Confusion: tp=29 fp=2 fn=18 tn=4 — same real pattern as before, skewed toward false negatives, `DEFAULT_THRESHOLD=1.7` still likely too conservative (unchanged diagnosis, still not re-tuned — see `agents/monitor/aggregation.py`'s own TODO). Real per-window latency this run: ~14-35s/window (both tiers stills now), sweep completed in 762.3s for 53 windows.

## Third pass (Aug 18) — full instrumented profile, every external boundary timed

The first two passes measured Monitor/Scene Graph Builder specifically, before the Aug 16 restructuring into Perception + Error Detection (`0930022`, `846ed3d`). This pass is the first full-system profile against the *current* architecture, and the first to separate Gemini time from Firestore time from local CPU time rather than reasoning about wall time alone.

**Method:** every external boundary the system crosses — `tools.adk_runner.run_llm_agent_once` (every Gemini call funnels through this one function), `tools.state_tools.apply_state_patch(es)` (Firestore writes), `tools.state_tools.get_state_snapshot` (Firestore reads), `tools.video_utils.sample_frames` (local frame decode), `tools.europepmc_rag.search_literature`, `tools.fhir_alert.send_alert`, `tools.fhir_write.write_document_reference` — was wrapped with a timer *before* the agent modules import (they hold `from tools.x import y` references, so patching after import would miss them). Nothing stubbed: this is the real production path, `agents.orchestrator.agent.open_case`, against the real running `state_service`, real Gemini, real Firestore (`surggraph-test`).

**Run:** `case-6f56d954a6ab`, `video_01` bounded 80→115s (35s of video, 7 non-overlapping 5s windows — `SURGGRAPH_SWEEP_START_S`/`_END_S`). **571 instrumented calls recorded.**

### Where the wall time goes

**WALL: 328.8s = 9.4× real-time**, for 35s of video.

`busy` = wall-clock seconds during which at least one call of that kind was in flight (categories overlap, so this doesn't sum to 100%):

| Category | calls | items | sum(s) | busy(s) | %wall | p50 | max |
|---|---|---|---|---|---|---|---|
| **gemini** | 211 | — | 2061.2 | 281.2 | **85.5%** | 6.7s | 55.5s |
| **firestore_write** | 111 | 371 | 225.5 | 122.3 | 37.2% | 1376ms | 6065ms |
| **firestore_read** | 76 | — | 104.7 | 77.7 | 23.6% | 1326ms | 5225ms |
| frames_cpu | 154 | 623 | 44.8 | 44.8 | 13.6% | 264ms | 666ms |
| europepmc | 17 | — | 26.0 | 8.6 | 2.6% | 1256ms | 3371ms |
| fhir | 2 | — | 3.0 | 3.0 | 0.9% | 1508ms | 2010ms |
| *unaccounted* | | | | 23.8 | 7.2% | | local Python, asyncio waits, event-bus polling sleeps |

2061s of real Gemini work compressed into 281s of wall via concurrency — 7.3× effective parallelism, doing its job. The problem is volume, not scheduling.

### Lever 1 — Error Detection's deep pass: 1203s, 58% of all LLM time, the single largest item

```
ERROR DETECTION = 1558s of 2061s total LLM time (76%), 147 of 211 calls
   screen pass :  21 calls    355.4s
   deep pass   : 126 calls   1203.0s   <- unconditional: all 6 categories x 3 roles, every window
```

The "Explored" section above (Aug 14) records making the deep pass unconditional deliberately — `max(screen, deep)` wall time instead of `screen + deep`, at the cost of always running 18 calls/window instead of only the escalated category. That tradeoff still holds for wall time within one window, but at system scale it means every window pays for 6 categories × 3 roles regardless of whether anything actually escalated, and it is now confirmed to be the largest single cost in the system — larger than every other category combined. **Not yet acted on** — re-introducing escalation (deep pass only the top-suspected category, dropping the unconditional design) is the highest-leverage remaining lever and is a real design reversal, not a tuning knob, so it wants a deliberate decision rather than folding into this measurement pass.

### Lever 2 — 69 single-item Firestore writes: 115.4s, cheaply recoverable

```
single-item writes :  69 calls  sum=115.4s  p50=1015ms
batched writes      :  42 calls  sum=110.1s  (302 items, 365ms/item)
-> the same 69 items in ONE batch would cost about 2.4s
```

Call sites on the live path: `agents/error_detection/agent.py` (6 — each sub-agent's edge written as its own transaction), `agents/perception/writer.py` + `agents/perception/agent.py` (6), `agents/scene_graph_builder/agent.py` and `agents/anticipation/agent.py` (legacy, currently unused on the live path — see `docs/HANDOVER.md`). **~113s recoverable, no reasoning changes.**

Decomposed in isolation (separate micro-benchmark, `surggraph-test` database, WSL2 → `us-central1`):

```
plain .set()  (one round trip, no transaction) : p50  337ms   <- base network RTT
transactional single write                     : p50 2523ms
-> transaction overhead                          2186ms (87% of the write)
   (begin + read case doc + 2 writes + commit = ~4 sequential round trips vs 1)

batch of 20 items in ONE transaction: 2481ms total = 124ms/item (20.3x faster than 20 singles)
```

**The HTTP/FastAPI layer is not the cost** — overhead measured ~0ms on writes and ~1% on reads in the same micro-benchmark. It is Firestore round trips, not the service wrapping them.

### Lever 3 — 58% of wall time is after the video sweep finishes

```
video sweep  (frame decode active) :   0.0 -> 137.2s   (42% of wall)
post-sweep   (drain + post-case)   : 137.2 -> 328.8s   (58% of wall)

DIVERGENCE polling: 33 LLM calls, 157s, spanning t=151s -> 302s
```

`agents/divergence_detection/agent.py`'s `POLL_INTERVAL_S=8 × MAX_POLLS=12` allows up to 96s of polling per proposal; the orchestrator's drain budget is `MAX_POLLS*POLL_INTERVAL_S+30 = 126s`. This run hit it: the log shows `"2 handler(s) exceeded the 126s drain budget, cancelling"`. Spending 192s of tail time on a 35s clip is disproportionate to the sweep itself, and unlike Lever 1 this is pure waiting, not reasoning work — the cheapest wall-time win in the system once writes are batched.

### Lever 4 — Firestore base RTT: 337ms on every graph operation

371 graph items and 76 snapshot reads all pay the same ~337ms WSL2 → `us-central1` round trip. A co-located Cloud Run deployment (or a local emulator for dev iteration) removes it; this compounds with Lever 2 since the transaction pattern multiplies the RTT by ~4.

### Two incidental findings

- **Snapshot reads are slower under concurrent load than in isolation** — p50 1326ms during the live run vs 760ms in the standalone micro-benchmark. Concurrent writers to the same case contend on the shared `seq` document.
- **A configuration error reports as retryable contention, not as a config error.** When the `(default)` Firestore database didn't exist (only `surggraph-test` does — see §11.3 of `docs/plan_v2_autonomous_safety_system.md`), `services/state_service/store.py::apply_patch`'s `except ValueError` caught the transaction wrapper's rollback failure and raised `TransactionContentionError` → HTTP 503 "retry". A 404 that will never succeed on retry surfaces identically to real, transient contention. Cost real debugging time in this session; worth narrowing the except clause before this is ever deployed somewhere a missing-database misconfiguration is more likely.

### Caveats

One run, 7 windows, bounded 80–115s — not a multi-run statistical sample. Gemini tail latency is wide (p50 6.7s, max 55.5s; the first window in any sweep pays a cold-start tax), so per-agent *medians* above are more trustworthy than the maxima. The isolated Firestore micro-benchmark showed one unexplained anomaly across two runs — direct in-process transactional writes measured *slower* than the same write over HTTP in one run (2523ms vs 1013ms) — plausibly a warm-vs-cold gRPC channel difference in the long-running service process, not reproduced deliberately. The base-RTT/transaction-overhead decomposition (337ms vs 2523ms, the 87% figure) was stable across both runs and is the number to trust.

### Current state (Aug 18) — nothing in this pass has been acted on yet

This was a measurement pass only; no code changed. Ranked by recoverable wall time for the effort involved:

| Lever | Recoverable | Cost to fix | Status |
|---|---|---|---|
| Batch Error Detection / Perception's single-item writes | ~113s | Low — mechanical, no reasoning change | Not started |
| Tighten divergence-detection poll/drain budget | up to ~90s of the 157s | Low — tune two constants | Not started |
| Re-introduce Error Detection escalation (drop unconditional deep pass) | up to ~1000s | Medium — reverses a deliberate Aug 14 design decision, needs its own accuracy check | Not started, needs a decision |
| Co-locate Firestore region / use emulator for dev | ~337ms × (371 writes + 76 reads) per case | Medium — infra, ties into the deferred Cloud Run deployment (plan §11.3) | Not started |
| Narrow the `except ValueError` in `store.apply_patch` to not mask config errors as contention | N/A (correctness, not speed) | Low | Not started |

Full raw event data (571 timed calls, per-call start/end/duration) preserved at `/tmp/claude-1000/.../scratchpad/profile_events.json` for this session only — not committed to the repo; re-run the same instrumented script against a fresh case to reproduce.

## Fourth pass (Aug 18) — five priority levers from the third pass, landed one at a time with real before/after measurement

Each priority below is independently shippable; the third-pass instrumentation (`tools/adk_runner.py`'s per-call timing wrap in a session-local profiling script, now also `USAGE_LOG`) is reused rather than rebuilt. Priority 4 (shard the `seq` document) was explicitly deferred by the user — not started, not measured.

### Priority 1 — Context caching: measured, not adopted, with a real architectural reason why

**Step 1 — does implicit caching fire?** `tools/adk_runner.py::run_llm_agent_once` now captures `event.usage_metadata` (confirmed directly on the installed ADK 2.6.3's `Event` model — `usage_metadata: GenerateContentResponseUsageMetadata | None`) into a module-level `USAGE_LOG`, logged per call. Ran the same bounded live case as the third pass (`video_01`, 80→115s, `open_case` end to end).

**Result: 0.00% cache-hit ratio.** 169 real Gemini calls, every one reporting `cached_content_token_count = None`/0. `sum(prompt_token_count)` across all calls: 819,413; `sum(cached_content_token_count)`: 0. Confirms the user's own stated hypothesis — implicit caching is not firing.

**Step 2 — why, and can explicit caching fix it?** Investigated ADK's real explicit-caching mechanism directly against the installed source (`google/adk/flows/llm_flows/context_cache_processor.py`, `google/adk/models/gemini_context_cache_manager.py`) rather than assuming the documented API works as described. Two real, independent blockers found:

1. **Session-lifecycle mismatch.** ADK's `ContextCacheRequestProcessor` finds cache metadata by scanning `invocation_context.session.events` for a prior call's `cache_metadata` (`_find_cache_info_from_events`). `run_llm_agent_once` creates a brand-new `InMemoryRunner` **and** a brand-new session on every single call (`await runner.session_service.create_session(...)`) — so `session.events` is always empty at request time, `cache_metadata` is always `None`, and `GeminiContextCacheManager.handle_context_caching`'s own comment confirms the consequence: *"No existing cache metadata — return fingerprint-only metadata. We don't create cache without previous fingerprint to match."* As wired today, explicit caching can never activate — every call takes the fingerprint-only branch, forever, regardless of `ContextCacheConfig` settings. Fixing this requires restructuring the caller to reuse one session across every window's call to the same cacheable-agent identity, a real, non-trivial change to `run_llm_agent_once`'s call pattern.
2. **The cacheable content is too small to clear the real token floor, independent of (1).** The installed SDK hardcodes `_GEMINI_2_5_MIN_CACHE_TOKENS = 2048` and `_GEMINI_3_MIN_CACHE_TOKENS = 4096` — real, vendor-defined explicit-cache minimums, not assumed. Measured the real static-instruction token count via `client.models.count_tokens` (no frames, just the OCHRA + role-framing text every call sends): screen instructions run 1054–1062 tokens, deep instructions (single-category OCHRA block) run 384 tokens. **All of them are under a third of the 4096-token floor.** Explicit caching cannot activate for these prompts even with perfect session-reuse wiring — the static prefix is architecturally too small.

**Decision: not implemented.** Fixing blocker (1) alone would be real engineering spent on a mechanism blocker (2) proves can never fire. Padding the static instruction purely to clear a caching threshold was considered and rejected without asking first — it would add real per-call token cost and risk shifting model behavior, for a caching benefit that isn't guaranteed to net positive, and the cross-cutting rule here is no accuracy-affecting change ships unmeasured.

**Verdict table, matching the "Explored" format used elsewhere in this doc:**

| Option | Verdict | Why |
|---|---|---|
| Implicit (automatic) caching | **Confirmed not firing** | 0/169 real calls, measured directly via `usage_metadata.cached_content_token_count` |
| Explicit `ContextCacheConfig`, current `run_llm_agent_once` session pattern | **Rejected as-is** | Session-event-based cache lookup structurally can't find a prior cache when every call gets a fresh session — confirmed against ADK source, not assumed |
| Explicit `ContextCacheConfig` + session-reuse restructuring | **Rejected** | Even with correct wiring, the real static prefix (384–1062 tokens) is under 4096, Gemini 3's real explicit-cache floor (confirmed from the installed SDK's own constant) — the mechanism cannot activate regardless of session plumbing |
| Pad the static prefix to clear the token floor | **Not attempted, flagged rather than done unasked** | Real added per-call cost and accuracy risk for an unproven caching win; out of scope without an explicit decision to spend that budget |

No code shipped for this priority beyond the measurement instrumentation itself (kept in `adk_runner.py` — costs nothing per call, and the same question is worth re-asking if a future prompt restructuring changes prefix size or call pattern).

### Priority 2 — Re-introduced escalation, tiered the screen pass

**Step 1 — escalation restored.** `agents/error_detection/aggregation.py::pick_escalation_candidate` already existed, fully unit-tested, simply unwired since the Aug 14 unconditional restructuring — not reimplemented, just reconnected. `agents/error_detection/coordinator.py::run_error_detection_window` now runs screen first (always, all 6 categories, all 3 roles — unchanged), then calls `pick_escalation_candidate` on the screen pass's own per-role confidences, and only escalates to a deep pass for that ONE category (3 calls: 3 roles × 1 category) instead of all 6 unconditionally (18 calls: 3 roles × 6 categories). For every category that does NOT escalate, the screen pass's own per-role `suspected`/`confidence` stands in as that category's verdict, aggregated through the identical weighted formula (`aggregate()`) used for deep results — this is not a new invention: `pick_escalation_candidate`'s own docstring, predating this change, already documented it as the design's fallback ("the screen-pass booleans stand in as the final O values"). A new `CategoryResult.reviewed: Literal["deep","screen"]` field distinguishes the two so nothing downstream can mistake a screen-only opinion for a focused deep-tier review — `ErrorDetectionSubAgentAssessment.tier_used` is a closed 3-value enum with no "screen" slot, kept at the fixed deep tier for schema/frontend compatibility (confirmed nothing currently reads `tier_used` downstream) with the real distinction carried in `reviewed` and a `[screen-pass, not deep-reviewed]` prefix on the fallback reasoning text instead.

**Real, disclosed structural cost:** screen and deep now run sequentially (screen must complete before the escalation choice can be made), reversing the "second pass" (Aug 14) change to `max(screen, deep)` concurrency. Accepted because deep dropped from 18 calls to ≤3 — net call volume and net latency both still improve, measured below.

**Step 2 — the real accuracy check, before shipping.** `git stash` isolated the unconditional (pre-change) coordinator for a clean "before" run; both sweeps used identical parameters (`video_01`, `--start-s 0 --end-s 90 --stride-s 5 --window-s 5.0`, 18 windows) for direct comparability, via `scripts/run_error_detection_validation_sweep.py`.

| | before (unconditional) | after (escalation) | delta |
|---|---|---|---|
| **macro_f1** | 0.498 | **0.699** | **+0.201** |
| raw_accuracy | 0.500 | 0.722 | +0.222 |
| f1 (error+) | 0.526 | 0.783 | +0.256 |
| precision (error+) | 0.833 | 0.900 | +0.067 |
| recall (error+) | 0.385 | 0.692 | +0.308 |
| confusion (tp/fp/fn/tn) | 5/1/8/4 | 9/1/4/4 | fn nearly halved |
| sweep wall time | 253.2s | 187.0s | −26% |

**Accuracy went UP, not down** — the opposite of the risk the Aug 14 "Explored" table flagged when it adopted unconditional deep review. Plausible real reason, not verified further given the small sample: spreading the fixed-tier deep reasoning across all 6 categories every window may have diluted focus relative to giving the screen pass's own triage a real, single-category, tier-framed second look — and the screen-derived fallback for non-escalated categories evidently isn't a meaningfully worse signal than an unconditional deep pass was providing for those same categories. Real caveat: n=18 windows, one video, one stride/window configuration — not the full 262-window CARES-style sweep, and not restated as a general claim about escalation vs. unconditional review beyond this dataset. Archived: `data/validation/error_detection_accuracy_2026-08-18_{before,after}_escalation.jsonl`.

**Step 3 — screen pass tiered to `gemini-3.5-flash-lite`, measured, then reverted.** Model availability was verified directly against this project's real Vertex AI endpoint, not assumed: `client.models.get(model="gemini-3.5-flash-lite")` resolves to a real publisher model (`gemini-3.5-flash-lite-preview` and `gemini-3-flash-lite` do not exist — confirmed 404 on the same check), and a live minimal generate call through the same `global`-location `GlobalGemini` wrapper the rest of the system uses succeeded (1.81s, real response). Wired as `tools.gemini_model.GEMINI_SCREEN_MODEL`, used only by `build_subagent(mode="screen", ...)`, isolated from the escalation change per the cross-cutting rule — a separate validation sweep, identical parameters, compared against the "after escalation" (full-model) run rather than the original unconditional baseline.

**Real result: the entire accuracy gain escalation had just won was erased.**

| | escalation, full model | escalation, lite screen | delta |
|---|---|---|---|
| **macro_f1** | 0.699 | **0.498** | **−0.201** |
| raw_accuracy | 0.722 | 0.500 | −0.222 |
| recall (error+) | 0.692 | **0.385** | **−0.308** (nearly halved) |
| confusion (tp/fp/fn/tn) | 9/1/4/4 | 5/1/8/4 | fn nearly doubled |
| sweep wall time | 187.0s | 115.7s | −38% |

0.498 is, to three decimal places, exactly the pre-escalation unconditional baseline's macro_f1 — the lite model's weaker triage judgment cancels out escalation's entire win, not merely erodes it. The screen pass's real job is deciding WHICH category is worth a deep look; a weaker model there means real errors are missed at triage and never reach deep review at all, which is a more damaging failure mode than a weaker model reviewing something already flagged. **Reverted** — `agents/error_detection/subagents.py`'s screen branch is back on the full model, and `GEMINI_SCREEN_MODEL` was removed from `tools/gemini_model.py` entirely rather than left as unused, defaulted-off plumbing. The −38% latency win was real but not worth trading away the larger, harder-won accuracy gain from Step 1/2 for it.

**Step 4 — combined before/after, same window as the third pass.** Ran the full instrumented `open_case` profile (reusing the third-pass wrapper unchanged) on the identical bounded window (`video_01`, 80→115s, 7 windows), with Priority 2 (escalation, full model — Step 3's lite-tier reverted, see above), Priority 3 (write batching, below), and Priority 5 (poll tightening, below) all landed together. This table reflects the actual shipped configuration — an earlier version of this run included the since-reverted lite screen model and was replaced once Step 3 was rejected, rather than left standing as a stale "current state" number. Isolating every priority into its own full end-to-end run was not practical inside this session's time budget; Priority 2's own accuracy delta was isolated separately via the dedicated validation sweeps above, since accuracy is the one dimension that genuinely needed clean isolation regardless.

| | third pass (baseline) | fourth pass (shipped: P2+P3+P5) | delta |
|---|---|---|---|
| **wall time** | 328.8s | **217.8s** | **−34%** |
| Gemini busy(s) / %wall | 281.2s / 85.5% | 176.3s / 81.0% | both dropped |
| Error Detection LLM calls | 147 | **42** | −71% |
| Error Detection LLM time | 1558.0s (76% of all Gemini time) | **437.0s (58% of all Gemini time)** | −72% |
| Error Detection calls/window | 21 (3 screen + 18 deep, unconditional) | **6** (3 screen + 3 deep, escalated) — confirmed real: 21 screen + 21 deep calls / 7 windows | matches the design exactly |
| Firestore write calls | 111 (69 single + 42 batch) | **40** (1 single + 39 batch) | −64% calls |
| Firestore write time | 225.5s | **119.3s** | −47% |
| Post-sweep tail | 191.6s (58% of wall) | 104.2s (48% of wall) | −46% absolute |
| Drain-budget cancellations | yes ("2 handler(s) exceeded the 126s drain budget, cancelling") | **none** | real regression from the third pass, gone |
| Divergence-detection LLM calls | 33 calls / 157.0s | 20 calls / 99.7s | −40% calls, −36% time |

**Graph chain re-validated after all shipped changes**, on the real case this run produced (`case-1d1f64683f70`): `scripts/validate_graph_chain.py` — **0 dangling edges, 0 orphan nodes, 0 unreachable from trigger**, every applicable relation present (perception → error → complication → corrective_trajectory → benchmark/documentation all connected). Confirms the write-order guarantee within a batch (node before its own edge, same transaction) held under the restructured `on_window_complete`, and that escalation's restructuring didn't break anything downstream. Run twice across this pass (once with the lite screen model still active, once after reverting it) — both passed cleanly, so this isn't a one-off.

Every number in this table moved in the right direction on this run, including divergence-detection call count/time, which is real but should be read as one data point, not a guarantee: a different run's proposal count depends on what errors actually get detected, which varies with real content and real model output, not just with these two constants.

### Priority 3 — Batched Error Detection's single-item writes

`tools/state_tools.py::apply_state_patches` already existed (a real Firestore `WriteBatch`-backed batch transaction, not N sequential ones — confirmed in the third pass: 20.3× faster per item) and was already used by Perception's own main per-window write path (`agents/perception/agent.py::_emit`, fully batched, predating this change) — so this priority's real scope turned out narrower than the third pass's file-level `grep` suggested. Checked precisely, not assumed: `agents/perception/writer.py`'s four single-write async wrappers (`upsert_entity`, `write_event`, `update_snapshot_slot`, `write_phase_node`) have **zero live callers** anywhere in the codebase (confirmed via import-level grep, not just call-site grep) — genuinely dead code superseded by the batched patch-builder functions `_emit` already uses, so touching them would have been scope creep against content nothing runs. `agents/perception/agent.py::_ensure_agent_node` is a real single-item write with nothing to batch it against (called once per case, one node, no sibling writes in that step) — left as `apply_state_patch`, per the explicit instruction not to force artificial batching where there is nothing to batch.

The real, un-batched hot-path cost was entirely in `agents/error_detection/agent.py`: `_ensure_agent_nodes` (7 single writes, once per case: 4 agent nodes + 3 hierarchy edges) and `on_window_complete` (4 widened-range writes + 3 sub-agent edges + up to 2 per fired divergence, every window). Both converted to collect into one list and call `apply_state_patches` once — `_ensure_agent_nodes` once per case, `on_window_complete` once per window.

**Before/after**, same combined run reported under Priority 2 Step 4 above (this file's write pattern is orthogonal to which Gemini model/escalation logic runs, so this number is valid on its own even though the run also carries Priority 2's changes):

| | third pass | fourth pass | delta |
|---|---|---|---|
| Firestore write calls | 111 | 40 | −64% |
| — single-item calls | 69 | 1 | −99% |
| — batch calls | 42 | 39 | (perception's own batching, unchanged) |
| Firestore write time | 225.5s | 119.3s | −47% |

The one remaining single-item write is `_ensure_agent_node` (perception, correctly left single, see above) plus whichever legitimate single-item calls exist in modules outside this priority's stated scope (HITL, Verification Gate, Alert Routing, Corrective Replanning, Complication Reasoning, Documentation, Benchmark — none named in the priority spec, none touched here).

### Priority 5 — Tightened divergence-detection poll/drain budgets

`agents/divergence_detection/agent.py`: `POLL_INTERVAL_S` 8.0 → 5.0 (the fast end of the doc's own `agentic_workflow.md` §3 "polls every 5-10s" range, not outside it), `MAX_POLLS` 12 → 8 (max monitoring window per proposal: 96s → 40s). The orchestrator's drain budget (`agents/orchestrator/agent.py`) is derived from these two constants (`MAX_POLLS * POLL_INTERVAL_S + 30`), so tightening them here tightened it too without a separate edit: 126s → 70s.

**Considered, not built:** event-driven divergence detection (subscribing to new Perception/Error Detection events instead of polling on a fixed interval) would remove this cost category rather than shrink it, per the priority brief's own suggestion. Not attempted this pass — a real architectural change (the agent is currently activated once by a proposal's appearance and then polls independently; making it react to every new perception/error event would change its activation model, not just its constants) and out of scope for a "land one change at a time, keep it isolated" pass. Flagged here as a real follow-up, not silently dropped.

**Before/after:** the drain-budget ceiling was hit in the third-pass baseline run (explicit cancellation log line) and was not hit in the fourth-pass shipped-configuration run (Priority 2 Step 4's table, above). Divergence-detection LLM call count and time both dropped in the same comparison (33→20 calls, 157.0s→99.7s) — a real result, but reported as one data point rather than a guaranteed effect size: an earlier run taken mid-pass (with the since-reverted lite screen model still active) showed the OPPOSITE direction on this specific metric (33→47 calls), because that run's escalation logic detected more real errors and so generated more corrective proposals, each spawning its own poll loop — more proposals × a shorter budget each can still exceed fewer proposals × a longer budget. Proposal count depends on real detection output, which varies run to run; the poll/drain tightening's own effect (shorter ceiling per proposal, budget no longer breached) is the part that held consistently across every run this pass, and is what this priority actually controls.


### Current state (Aug 18, end of Fourth pass)

Supersedes the "Current state" table at the end of the Third pass section above (left in place as the historical snapshot of what was true before this pass started — this doc's own convention is to add new dated state, not rewrite old entries).

| Lever | Result | Status |
|---|---|---|
| Batch Error Detection's single-item writes | 111 → 40 write calls (−64%), 225.5s → 119.3s (−47%) | **Done** |
| Re-introduce Error Detection escalation | 1558.0s → 437.0s Error Detection LLM time (−72%), 147 → 42 calls (−71%); macro_f1 **improved** 0.498 → 0.699 (+0.201), not merely held even | **Done** |
| Tier the screen pass to a lighter model | −38% sweep wall time, but macro_f1 0.699 → 0.498 (erased the entire escalation gain) | **Rejected, reverted** — real numbers in Priority 2 Step 3 above |
| Tighten divergence-detection poll/drain budget | Drain-budget ceiling no longer hit (was hit in the third-pass baseline); 157.0s → 99.7s in the final shipped-config run (see Priority 5's own caveat on run-to-run variance) | **Done** |
| Event-driven divergence detection (replace polling entirely) | Not attempted — real activation-model change, out of scope for this pass | **Follow-up, not started** |
| Verify/adopt context caching (implicit or explicit) | 0.00% implicit cache-hit rate, measured on 169 real calls; explicit caching confirmed architecturally blocked (session-lifecycle mismatch) AND token-floor-blocked (384–1062 real static tokens vs. Gemini 3's real 4096-token minimum) | **Investigated, not adopted** — real vendor-source-confirmed reasons, not a guess |
| Shard the `seq` document (remove the Firestore write/read hotspot) | — | **Skipped this pass, per explicit instruction** |
| Prime call to warm the Vertex AI serving path before real sweeps dispatch | Built and shipped as directed; isolated A/B (identical code, prime call on vs. off) showed no clear benefit — 20.4s vs 19.7s real call max, within run-to-run noise. The third pass's 55.5s outlier this was meant to fix did not reproduce on either side of the comparison, most likely because Priority 2's own concurrency cut (21→6 calls/window) already removed most of what caused it | **Shipped, real benefit unconfirmed** — see Fifth pass |
| Co-locate Firestore region / use emulator for dev | ~337ms real base RTT per operation, unchanged | Not started |
| Narrow the `except ValueError` in `store.apply_patch` to not mask config errors as contention | — | Not started |

**Combined, measured effect on the third-pass's own comparison window** (`video_01`, 80→115s, real `open_case`, nothing stubbed): **wall time 328.8s → 217.8s, a real 34% reduction**, with zero drain-budget cancellations (present in the baseline) and the graph chain independently re-validated as fully connected (0 dangling/orphan/unreachable) on two separate real cases produced during this pass.

Raw event data for every run this pass produced (third-pass baseline, before/after escalation accuracy sweeps, before/after lite-screen accuracy sweep, two combined end-to-end profiles) preserved under `/tmp/claude-1000/.../scratchpad/` for this session only, plus the four accuracy sweep logs committed to `data/validation/error_detection_accuracy_2026-08-18_{before,after}_escalation.jsonl` and `error_detection_accuracy_2026-08-18_after_lite_screen.jsonl`.

## Fifth pass (Aug 18, same day) — prime call to warm the Vertex AI serving path before the real sweeps dispatch

Follow-up ask, same day as the fourth pass: the third pass's own numbers (211 real Gemini calls, p50=6.7s but max=55.5s) were read as a possible cold-start tax on the first real call in a sweep, and asked to be addressed with a tiny warm-up call at case open, before the real sweeps dispatch.

**Built.** `agents/orchestrator/agent.py::_prime_gemini` — one throwaway `LlmAgent` (`_PRIME_AGENT`, built once at import) using the identical model/location configuration every real sweep agent uses (`tools.gemini_model.new_agent_model`, the `global`-location `GlobalGemini` wrapper), fired through the same `run_llm_agent_once` every real call goes through. Wired into `open_case` via `asyncio.gather(_draw_static_hierarchy(...), _prime_gemini(case_id))` — concurrent with the (Firestore-only) skeleton write, not sequential in front of it, since the two share no real resource and there's no reason to pay both latencies back to back. Best-effort: caught and logged, never blocks or fails case open if the prime call itself errors or times out — matching every other non-critical failure path in this module (`docs/agentic_workflow.md` §10).

**Real measurement, isolated properly.** The third pass's 55.5s max came from a now-superseded architecture (unconditional deep pass, up to 21 concurrent Error Detection calls/window) — comparing against it directly would conflate the prime call's own effect with everything Priority 2–5 already changed. Instead: two runs of the identical current shipped code (escalation + batching + tightened polling), same 80→115s window, differing in exactly one thing — the prime call monkeypatched to a no-op for one of them.

| | no prime (isolated) | with prime | delta |
|---|---|---|---|
| real Gemini call max | 20.4s | 19.7s | −0.7s, within noise |
| real Gemini call p50 | 9.3s | 7.5s | −1.8s, within noise |
| wall time | 176.7s | 199.7s (or 217.8s on an earlier same-config run) | no clean signal — case-to-case variance from real content (different real detections → different downstream reasoning volume) swamps a ~3s prime call either way |

**Honest read: no clear benefit measured.** Neither run reproduced anything close to the third pass's 55.5s outlier — both landed around 20s max. The most likely explanation: that outlier was a real symptom of the *old* architecture's much higher concurrent call volume (up to 21 Error Detection calls/window competing for Vertex AI's Dynamic Shared Quota pool at once), not a distinct "cold path" phenomenon separate from load. Priority 2's escalation change already cut that concurrency by ~70% (21 → 6 calls/window) independent of this pass, which plausibly fixed most of what caused the spike before priming ever got a chance to matter.

**Shipped anyway, disclosed rather than silently reverted.** This was a direct, specific implementation request, not a hypothesis this pass was free to reject the way Priority 2 Step 3's model tiering was (that one shipped a measured *regression*; this one shows an unmeasured, unconfirmed benefit — a materially different case). The prime call is cheap (≈3s, paid once, overlapped with the skeleton write rather than purely serial), safe (best-effort, cannot fail or block the real sweep), and does what was asked. Kept in place. If a future pass wants to settle this more rigorously, it needs repeated runs (n≫1 each side) to separate a real few-second effect from this level of case-to-case variance — a single A/B on a live, variable-latency cloud API is not enough to confirm or rule it out cleanly, and this doc says so rather than reporting a false win.

**Graph chain re-validated a third time** on the real case this pass's "with prime" run produced (`case-60ca1d600195`): `scripts/validate_graph_chain.py` — passed cleanly, same result as the two Fourth-pass runs. The prime call's own graph write (none — it never touches the graph, it is a pure Gemini round trip with no `apply_state_patch` call) confirmed by inspection, not just by the chain validator's silence.

**Tying back to the headline number:** the Fourth pass's "Combined, measured effect" figure (328.8s → 217.8s, in the Current state table above) predates this pass and does not include the prime call. The one run that includes everything shipped through this pass — escalation, batching, tightened polling, *and* the prime call — measured **199.7s** wall on the same window, consistent with (in fact faster than) 217.8s given the run-to-run noise already documented above. Not restated as the new headline number on its own, since a single run isn't a stronger claim than the 217.8s figure already made from its own single run — both are real data points from the same noisy real-world range, not two independently confirmed numbers.

