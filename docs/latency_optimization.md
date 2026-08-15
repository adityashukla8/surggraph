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
