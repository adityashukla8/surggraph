# SurgGraph

Written 2026-08-15 for whoever picks this up next. Everything here reflects the actual state of the code as of commit `fb6098d` — not the aspirational plan. Where the plan and the code disagree, the code is described, and the gap is called out explicitly.

## 1. Project goal (functional)

SurgGraph watches a recorded robot-assisted radical prostatectomy (RARP) surgical video and, live, builds a running graph of what's happening — in real time, as the video plays — using genuine multi-agent LLM reasoning, not a scripted demo. Concretely, three things happen concurrently as soon as a case opens:

1. **Perception** — what surgical activity, instruments, and anatomy are actually visible, moment to moment.
2. **Error detection** — whether the observed technique deviates from expected practice, with a real reasoning trail per detection.
3. **Anticipation** — a live forecast of what's likely to happen next, made *before* the slower perception/detection agents get there, later confirmed or left honestly unconfirmed once real corroborating evidence lands.

All three stream their real findings onto a shared "Living State Graph" (nodes = agents/phases/entities/events, edges = relations/detections/predictions), visible live in a React/ReactFlow frontend as the video plays.

Built for the Google Cloud "All Things Agentic" hackathon (deadline Aug 31, 2026, 5PM PDT). The originally-scoped system is a 12-agent clinical-decision-support pipeline (complication prediction, imaging, literature retrieval, verification, FHIR write, Slack alerting) — **only the perception/detection/anticipation core above is built**. See §4 for exactly what exists vs. what's still an empty scaffold.

## 2. Approach & design principles

These aren't stylistic preferences — several were arrived at by catching and fixing a real design flaw mid-build, and re-violating them is the single easiest way to quietly regress this project's actual value proposition for a hackathon judged partly on genuine autonomous reasoning.

- **Zero-shot Gemini vision reasoning, no training.** Model is `gemini-3.5-flash` via Vertex AI — reachable *only* through the `global` location (every regional endpoint 404s for this project; confirmed directly, not a guess — see `tools/gemini_model.py`'s own docstring). No fine-tuning, no GPU, nothing trained on this dataset.
- **No hardcoding, no disguised deterministic workflow.** Every judgment call an agent reports (what phase this is, whether an error is present, what's likely next) must come from a live Gemini call. Deterministic Python is fine for orchestration, aggregation math, and bookkeeping — never for the actual decision content. (`agents/monitor/aggregation.py`'s weighted scoring is deterministic *arithmetic over Gemini's own real outputs* — that's a different thing from deciding the outputs themselves.)
- **Ground truth: structural signal yes, semantic answer no.** SAR-RARP50 ships real ground-truth annotations (phase IDs, segmentation masks, error labels). The rule that came out of catching two real violations of it this session: a real *opaque, unlabeled* structural signal (a bare numeric ID with no published name) may be fed to an agent as input context — it stands in for what a genuine upstream sensor would provide. A real *semantic, named* signal (the actual answer) must never reach a live decision path — it's validation-sidecar only, logged post-hoc to score accuracy, never shown to the model before it decides. Full history in §9.
- **Real-time streaming, not batch-then-render.** Agents write to the graph as each unit of work (a ~5s window) completes, not after the whole sweep finishes. This is what makes the graph read as live reasoning rather than a progress bar.
- **Latency is a first-class design constraint, explicitly traded against accuracy.** Real per-call Gemini latency is far slower than the video's own real-time playback; every latency decision this project has made accepts some real, disclosed accuracy cost in exchange (documented as it happens in `docs/latency_optimization.md` — read that file for the full history of what was tried, measured, and why).

## 3. Current status

**Non-negotiables** (from the original plan, "never slip"):

| # | Requirement | Status |
|---|---|---|
| 1 | Event-driven trigger | ✅ Done (simplified from GCS/Eventarc to a single frontend HTTP call on first play — see §9) |
| 2 | Fail-closed Verifier, visible blocks | ❌ Not built (`agents/verifier/` is an empty scaffold) |
| 3 | FHIR write + readback | 🟡 Built and tested standalone (`tools/fhir_write.py`, `tests/test_fhir_write_readback.py`), **not wired into any live path** — nothing currently calls it |
| 4 | Alerts from autonomous decisions | ❌ Not built — no alerting tool exists at all yet |

**Built and working:** Orchestrator, Monitor, Scene Graph Builder, Anticipation (§5), the Firestore-backed state service, the React/ReactFlow frontend, FHIR write (standalone, unwired).

**Empty scaffolds only** (`__init__.py`, 0 lines, nothing else — verified directly, not assumed): `agents/mission_planner/`, `agents/world_model/`, `agents/safety_critic/`, `agents/complication_enumeration/`, `agents/imaging/`, `agents/literature/`, `agents/verifier/`, `agents/recovery_planner/`, `agents/action_router/`, `agents/debrief/`. Their intended one-line purpose, from the plan:

- **Mission Planner** — picks the active plan from what World Model rehearsed and Safety Critic ranked.
- **World Model** — the one place hand-authored logic is legitimate; rolls a candidate trajectory forward on a copy of the graph.
- **Safety Critic** — ranks rehearsed futures by risk (likelihood × severity).
- **Complication Enumeration** — RAG-based, formulates its own literature query from current/anticipated phase; no hand-authored phase→complication table.
- **Imaging Agent** — real DICOM/TotalSegmentator tools against an MRI series, triggered on divergence.
- **Literature/RAG Agent** — reactive counterpart to Complication Enumeration.
- **Verifier** — fail-closed gate before any real-world write; read-only, blocks are visible structured events.
- **Recovery Planner** — post-confirmation literature-grounded recovery options.
- **Action Router** — the actual autonomous real-world decision (write/alert/no-action/escalate), gated on Verifier.
- **Debrief Agent** — lowest priority; the plan never fully specified its output.

**Pending:** no full-video (unbounded, default-settings) live run has been done yet. Monitor's and Anticipation's offline accuracy summaries *have* been re-run against the current code (§9, `docs/validation_results.md`) — check that file for real, dated numbers before assuming either is stale.

## 4. Agent cards

### Orchestrator — `agents/orchestrator/agent.py`

**Role:** the root entry point. Given a `case_id` and `video_id`, draws the static agent hierarchy on the graph, then dispatches the three real agents concurrently.

- `open_case(case_id, video_id, start_s=None, end_s=None)` — the actual work function. Defaults to the video's *entire* real duration (`find_video_duration_s`), not a bounded demo window.
- `_draw_static_hierarchy(...)` — writes the trigger node, all 3 top-level agent nodes, Monitor's 3 sub-agent nodes, and every hierarchy edge between them, **sequentially, before any agent sweep starts**. This exists because of a real bug (§9): each agent's own registration write is correct but was getting starved for minutes behind dozens of concurrent Gemini calls sharing Python's default thread pool once sweeps were running.
- Runs `monitor_case`, `scene_graph_case`, `anticipate_case` via `asyncio.gather` — concurrent, not sequential.
- `OrchestratorAgent(BaseAgent)` — the real ADK wrapper; `_run_async_impl` is a thin adapter, all real logic lives in the plain async functions above.
- `services/orchestrator_service/main.py` — `POST /cases/open`, mints a fresh `case_id`, schedules `open_case` as a FastAPI background task, returns immediately.

### Monitor Agent — `agents/monitor/{agent.py, coordinator.py, subagents.py, aggregation.py, knowledge.py}`

**Role:** live error detection, modeled on the published CARES architecture (arXiv:2508.08764) — three independent role-specialist sub-agents (Temporal, Spatial, Procedural) reasoning over the same window, combined by deterministic weighted aggregation.

- **Cascade, both tiers now stills** (changed this session, §9): a *screen* pass (3 calls, one per role, broad 6-category scan) and a *deep* pass (18 calls, 3 roles × 6 categories, unconditional — runs every window, not just escalated ones) run **concurrently** (`asyncio.gather`), both sampling the same local still frames (`STILL_FRAME_PROFILE`, `agents/monitor/subagents.py`: temporal=5 frames@960×540, spatial=4 native, procedural=3 native). Native video was dropped entirely from this agent this session — real, disclosed accuracy cost (it once caught an event stills missed), accepted for latency.
- `aggregation.py` — deterministic weighted composite score (`DEFAULT_ALPHA`: temporal > spatial > procedural) against `DEFAULT_THRESHOLD`; requires ≥2-of-3 agent agreement. Never a 4th LLM call.
- `knowledge.py` — the 6 real CARES error categories (Multiple Attempts, Out of View, Needle Handling, Tissue Handling, Suture Handling, Instrument Control), each with an OCHRA-grounded definition/indicators block embedded in every prompt — legitimately hand-authored domain scaffolding (fixed clinical doctrine, not something derivable from one video).
- A window can fire **zero, one, or several** real `DivergenceEvent`s (one per category that independently crosses threshold) — `build_divergence_events` in `coordinator.py`.
- Phase-node writes are create-if-missing only — never overwrites a real semantic label (from Scene Graph Builder or Anticipation) with Monitor's own generic `"Phase {id}"` fallback (Monitor has no general activity description of its own to offer).
- `MonitorCoordinatorAgent(BaseAgent)` — real ADK coordinator, not `LlmAgent`/`ParallelAgent` (the latter carries a real deprecation marker in the installed ADK version — confirmed directly, not assumed).
- Validation-only: `tools/sedmamba_labels.py` reads real SEDMamba ground-truth error labels — used exclusively by `scripts/run_monitor_validation_sweep.py` for offline macro-F1 scoring, never in the live decision path (a test, `test_coordinator_never_imports_ground_truth_in_decision_path`-style guard, enforces this).

### Scene Graph Builder — `agents/scene_graph_builder/{agent.py, subagent.py}`

**Role:** the agent that actually perceives *what's happening* — instruments, anatomy, their relations, and a plain description of the activity. Owns the graph's entity/relation nodes and the phase nodes' real semantic labels.

- One real Gemini call per ~5s window, over 5 local still frames at native resolution (switched from native video this session, same latency reasoning as Monitor).
- One real opaque structural signal fed as context: the window's real phase/action ID (no semantic name attached) — legitimate per §2's ground-truth rule; the model still has to do all the actual naming/reasoning work.
- Output schema (`SceneGraphWindowOutput`): `entities` (self-assigned stable `entity_id`s, idempotently tracked across windows), `relations` (subject→verb→target), `activity_description` (now becomes a graph node's literal label — kept short per this session's prompt-tightening, §9), `reasoning`.
- `_ensure_agent_node` registers its own agent node immediately at sweep start — this was a real regression this session (dropped during an earlier rewrite, silently meant the node only appeared once the first window's Gemini call returned) and had to be re-added.

### Anticipation Agent — `agents/anticipation/{agent.py, subagent.py}`

**Role:** dead-reckoning phase forecasting — predicts ahead of the other two (slower) agents' real observations, then reconciles against the live graph once real corroboration lands. This agent was rebuilt twice this session after catching two real design flaws; see §9 for the full story, it's worth reading before touching this agent.

- **Gemini never sees or produces a numeric phase ID, anywhere.** It sees real current-window stills plus an anonymized statistical hint (`tools/phase_transition_priors.py::summarize_transition_confidence` — real empirical transition-probability data, computed once offline from ground truth by `scripts/compute_phase_priors.py`, but stripped down to just "how consistent has this point historically been" with zero category labels before it reaches the prompt) and answers entirely in its own words.
- The real numeric phase ID (`phase_at_frame`) is used only by the Python wrapper — as the priors-lookup key and as the current-phase graph node's identity — never told to the model.
- Convergence: a predicted node/edge (`predicted-phase:{slug}`, slug = Gemini's own free-text prediction, normalized) gets its edge **redirected** onto the real node once an independent real agent's label text-matches it (`_reconcile_pending`, bounded ~120s poll). Text-match, not semantic equivalence — a real, disclosed limitation (two agents phrasing the "same" phase differently won't converge).
- Validation-only: `_log_anticipation_accuracy` logs predicted vs. real next-phase to `data/validation/anticipation_accuracy.jsonl` for offline scoring — never influences the live forecast or the live confirm/refute decision.

## 5. The graph layer

Full schema/wiring detail already written up in-conversation earlier this session (top-down: API → `state/schema.py` Pydantic models → Firestore collection layout → `tools/state_tools.py` read/write helpers → node/edge ID-based linking convention → frontend mirror types → `useCaseStateStream.ts` state management). The essential shape, for quick reference:

- A **node** is `{node_id, node_type, label, attrs, source_agent, source_tool, timestamp}`. A **edge** is `{edge_id, source_node_id, target_node_id, edge_kind, trajectory_id?, confirmation_signal?, reason}`. Nothing more nested.
- **Consumes:** every agent calls `tools/state_tools.py::apply_state_patch(case_id, node=..., edge=..., ...)` as its only way to touch the graph.
- **Creates/stores:** `services/state_service` (FastAPI, port 8080) persists to real Firestore (`cases/{case_id}/graph_items/{item_id}`, one collection for both nodes and edges, discriminated by a `kind` field; last-write-wins per item via `.set()`, never a delta).
- **Consumes it downstream:** the frontend (`ui/frontend/src/graph/useCaseStateStream.ts`) fetches an initial `GET /snapshot`, then subscribes to `GET /stream` (SSE), applying each diff with **per-node/per-edge** sequence tracking (not a global sequence check — a real bug fix this session, §9) into two `Map`s, filtered and dagre-laid-out for ReactFlow.
- **Linking has no foreign-key enforcement.** It works purely because every writer agrees on the same `node_id` string convention (`agent:{name}`, `phase:{opaque_id}`, `entity:{id}`, `predicted-phase:{slug}`, `event:{...}`) — whoever writes an edge just references a node_id someone else already used, or will use later.

## 6. API layer

```
services/orchestrator_service  (port 8090)
  POST /cases/open   { video_id }  ->  { case_id }

services/state_service  (port 8080)
  GET  /state/{case_id}/snapshot   -> StateSnapshot
  POST /state/{case_id}/patch      -> StateDiffEvent
  GET  /state/{case_id}/stream     -> SSE, event "state_diff"
  POST /events/manual              { case_id, text }  -> StateDiffEvent
  GET  /media/video/{video_id}/{filename}  -> raw video bytes (Range-aware, GCS-backed)
```
`state_service` is deliberately domain-agnostic — it knows nothing about surgery or agents, just a generic multi-tenant graph store + SSE relay.

## 7. Local dev setup

```bash
# Backend (from repo root)
uv run uvicorn services.state_service.main:app --host 127.0.0.1 --port 8080
uv run uvicorn services.orchestrator_service.main:app --host 127.0.0.1 --port 8090

# Frontend
cd ui/frontend && npm run dev   # http://localhost:5173
```
Required `.env` (repo root, gitignored — ask for real values, don't invent placeholders):
```
SURGGRAPH_PROJECT_ID, SURGGRAPH_REGION, SURGGRAPH_GCS_BUCKET
GOOGLE_GENAI_USE_VERTEXAI=true, GEMINI_MODEL=gemini-3.5-flash, GEMINI_LOCATION=global
FHIR_BASE_URL=https://hapi.fhir.org/baseR4
STATE_SERVICE_URL=http://127.0.0.1:8080
FIRESTORE_DATABASE=surggraph-test   # only real DB — "(default)" was never created, see §9
SLACK_WEBHOOK_URL, EUROPEPMC_BASE_URL
# SURGGRAPH_WINDOW_S=5.0            # optional override, defaults to 5.0
```
Real downloaded data lives in `data/video/video_01/`, `data/annotations/video_01/` (SAR-RARP50, one video — the "download one video" constraint from the original plan). `data/priors/phase_transition_matrix.json` is precomputed (`scripts/compute_phase_priors.py`), already checked in.

## 8. Testing

`uv run pytest tests/ -q` — 45 tests, all real-data assertions (no mocking the actual logic under test), a handful hit the real downloaded video/annotations directly. **What's not covered:** anything requiring a live Gemini call (the actual vision reasoning) is exercised only by real end-to-end runs, not unit tests — there's no mocked-Gemini test harness in this project by design (matches the project's own "hits the real thing" testing philosophy, e.g. `test_fhir_write_readback.py` hits the real public HAPI server). Frontend: `npx tsc --noEmit` from `ui/frontend/` for typechecking; no frontend test suite exists yet.

## 9. Known limitations, real gotchas, and why some decisions look the way they do

Worth reading before extending this system — several of these were caught the hard way.

- **The ground-truth rule exists because it was violated twice, for real.** Monitor's *original* design fired divergences by reading SEDMamba's real ground-truth error labels directly — a lookup table wearing an agent's name tag, caught before shipping. Anticipation repeated the same mistake independently (fed the real current phase ID straight to the LLM), then a first fix (labeled visual exemplar frames) fixed the input side but still produced a meaningless numeral as output. The final Anticipation design (§4) is the result of catching *both* failure modes. If you're adding a new agent, assume this same trap is easy to fall into and check twice.
- **Frontend seq-tracking bug (fixed this session):** Firestore's real-time listener delivers changes in whatever order it batches them, *not* guaranteed to match the app's own `seq` field. A global "next event must be exactly seq+1 or full resync" policy (the original design) treated normal reordering as a fatal gap once 3 agents wrote concurrently, causing frequent resyncs whose overlapping in-flight fetches could leave the graph looking stale or edge-sparse. Fixed with per-node/per-edge sequence tracking instead (§5) — if you touch `useCaseStateStream.ts`, understand why before reverting to something that looks simpler.
- **Thread-pool starvation (fixed this session):** cheap registration writes (`apply_state_patch`, dispatched via `asyncio.to_thread`) share Python's default thread pool with whatever the Gemini SDK's blocking I/O uses. Once dozens of long-running Gemini calls are in flight, a tiny write queued behind them can be delayed by minutes. This is why Orchestrator draws the entire static hierarchy *before* dispatching any agent sweep (§4) rather than trusting each agent's own first-line registration call.
- **`.env`'s `FIRESTORE_DATABASE` must be set** — `services/state_service/store.py` silently defaults to `"(default)"` if unset, which doesn't exist as a real database for this project (only `surggraph-test` does). A restarted service without this set will 500 on every real Firestore call. This bit us once already this session.
- **Latency-for-accuracy tradeoffs are real and disclosed, not silent.** Every agent now uses still frames, not native video — a real, measured ~2.9x per-call speedup, at the real cost of native video's motion-detection edge (it once caught an event stills missed). This was an explicit, repeated user priority (latency over accuracy), not a default anyone should assume applies elsewhere.
- **Anticipation's convergence is text-match, not semantic equivalence.** Two independent agents describing the "same" real phase with different wording will not converge on the graph. A stretch improvement (LLM-judged equivalence) was considered and deferred, not built.
- **Priors are single-video, genuinely thin.** `data/priors/phase_transition_matrix.json` is computed from exactly one downloaded video — some rows are `n=1` or `n=2` (a real statistic, not fabricated, but not a reliable pattern). Don't treat `coverage_n` as noise; it's the honesty signal.
- **The deeper design-decision history** (the CARES/SurgRAW research precedents, the full Firestore multi-tenancy debate, the exact rejected alternatives at each step) lives in a plan document that is **not part of this git repo** — it's local to the previous developer's tooling. If that history matters going forward, it should be exported into `docs/` rather than assumed to travel with the repo. `docs/latency_optimization.md` (real, checked in) has the full before/explored/current/after latency story and is the closest thing to that history that *is* in the repo.
- **`docs/validation_results.md`** is the dated, canonical record of every real accuracy/test run — check it before re-running something to "see if it's still accurate," and add a row to it every time you run a validation script for real. It was created because the previous accuracy numbers were scattered across code comments, and one of the two summarize scripts (`scripts/summarize_anticipation_accuracy.py`) had silently been broken (crashing on every run) since an earlier redesign changed the log schema underneath it without the script being updated — a real gap this file exists to prevent recurring.
