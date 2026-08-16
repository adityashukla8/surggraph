# SurgGraph — Agentic Orchestration Workflow

**The implementation reference for all agent work from here on.** Together with `docs/plan_v2_autonomous_safety_system.md`, this is **the plan** — not a delta on top of what exists. These two docs define the target system; the existing codebase is an asset pool to draw from, not a baseline to preserve. Reuse what fits, adapt what nearly fits, build what's missing, retire what the rescope made irrelevant.

- `docs/plan_v2_autonomous_safety_system.md` — **what** the system does (mission, node/edge vocabulary, 14-step workflow, perception data structures).
- This doc — **how**: who runs, when, triggered by what, with what concurrency/retry/failure discipline.

Older docs are historical record, not constraints on this plan: `docs/HANDOVER.md` (what the pre-rescope build was), `docs/validation_results.md` (real measured accuracy, still valid for the components that survive), `docs/latency_optimization.md`, and `~/.claude/plans/consider-the-initial-11082026-md-harmonic-cloud.md` (why the earlier design decisions were made — CARES grounding, ground-truth rules, Firestore multi-tenancy; the *principles* there still bind, the *scope* does not).

`docs/qa_log.md` records every real defect found by running the system, and how it was caught — worth reading before changing anything that writes to the graph.

§15 inventories what already exists and where it lands under this plan. §16 lists design decisions that need making before coding.

---

## 1. Roster at a glance

Three lifecycle classes — long-running sweeps, event-driven, post-case — plus two thin non-reasoning executors.

| # | Agent | Class | Trigger | Reasoning calls |
|---|---|---|---|---|
| 1 | Perception Sweep | long-running | case open | 1 per ~5s window |
| 2 | Error Detection Sweep | long-running | case open | up to 3 + 18 per window |
| 3 | Anticipation Sweep | long-running | case open | 1 per cycle + reconciliation |
| 4 | Complication Reasoning | event-driven | `error` node above severity | 1 per qualifying error |
| 5 | Literature Retrieval | event-driven (inline) | called by #4, #6 | 0 (tool wrapper) |
| 6 | Corrective Replanning | event-driven | `complication` node | 1 per complication |
| 7 | Trajectory Divergence Detection | event-driven (polling) | active un-dismissed proposal | 0–1 per check |
| 8 | Alert Routing | event-driven | `divergence_alert` node | 1 per confirmed divergence |
| 9 | Verification Gate | event-driven (synchronous) | called by #8, HITL #2 | 1 per proposed external write |
| — | Alert Executor | thin adapter | called by #8 after gate pass | 0 |
| — | FHIR Write Executor | thin adapter | called by HITL #2 after gate pass | 0 |
| 10 | Benchmark | post-case | case close | 0–1 |
| 11 | Documentation | post-case | case close, after #10 | 1 |

The numbering above is just an index into the roster — it carries no meaning beyond ordering, and the roster is what governs.

---

## 2. Long-running sweep agents

Dispatched at case open, run concurrently for the case duration via a single `asyncio.gather`. They share no state directly — only the graph, mediated by `apply_state_patch`.

### 1. Perception Sweep Agent

Per-window multimodal reasoning over still frames producing raw per-window perception output; the deterministic change-diff / debounce / rate-limit pipeline that converts raw output into entity updates and event nodes; snapshot-slot maintenance; audit-log writes.

- **Trigger:** once at case open; runs for case duration.
- **Reads:** current window stills; perception context slice (previous window activity, active entity IDs); opaque phase/action ID from the structural signal for this window.
- **Writes:** entity registry nodes (in-place); snapshot slots (in-place); event nodes when change-diff fires; perception raw audit-log entries (off-graph Firestore subcollection); own agent node's `last_active_window`.
- **Rate:** one reasoning call per ~5s window. Zero-to-few events per window depending on change; rate-ceiling enforced.

The deterministic pipeline (change-diff rules, 2-of-N debounce, EMA confidence smoothing, rate ceilings, 60s heartbeat, audit-log split) is specified in full in `docs/plan_v2_autonomous_safety_system.md` §7 — not repeated here. This reworks the existing Scene Graph Builder rather than replacing it — the reasoning call is close to what exists, the entire post-processing layer is new.

### 2. Error Detection Sweep Agent

The existing Monitor cascade, mechanically unchanged: CARES-style over the same 5s windows. Screen pass (broad, small) + deep pass (role × category matrix) running concurrently. Deterministic weighted aggregation with ≥2-of-3 role agreement above threshold.

- **Trigger:** once at case open; same window cadence as Perception but **independent of it** — it does not consume Perception's output for its own reasoning.
- **Reads:** current window stills at role-appropriate frame counts and resolutions; error-detection context slice (recent error history, current phase from snapshot); OCHRA-grounded category definitions from the embedded knowledge module.
- **Writes:** error nodes (append-only, one per triggering category per window). Never overwrites semantic phase labels written by Perception.
- **Rate:** one cascade per window — up to 3 (screen) + 18 (deep: 3 roles × 6 categories) concurrent reasoning calls, aggregated deterministically down to zero-to-several error emissions.

### 3. Anticipation Sweep Agent

Self-perception then forecasting: names the current phase in its own words from live stills, forecasts the next phase, calibrated by an anonymized transition-confidence hint. Never shown a numeric phase ID or a ground-truth name — that rule still binds.

Mechanics below carry over from the working implementation in `agents/anticipation/`; the open question is cadence (see [§16](#16-open-design-decisions)).

- **Trigger:** once at case open; runs for case duration on a fixed cadence.
- **Reads:** current window stills; anonymized statistical-shape hint derived in Python from the empirical transition priors; state snapshot for reconciliation.
- **Writes:** phase nodes labeled with its own semantic description; predicted-phase nodes keyed by slugified free text; prediction edges. Appends to the anticipation accuracy validation sidecar.
- **Rate:** one reasoning call per cycle, plus a bounded-time background reconciliation coroutine that flips predictions to confirmed/refuted.
- **Idempotency key:** `(case_id, prediction_id)` — deduplicated by the slug of the free-text prediction plus the current phase.

---

## 3. Event-driven agents

Fired by graph-change subscriptions (§5). Each handler runs as its own `asyncio.Task`.

### 4. Complication Reasoning Agent

On any error node above severity threshold: formulate a literature query, trigger retrieval, then reason over the triggering error + patient twin + vitals + literature to emit complication candidate nodes.

- **Trigger:** subscription on `node_type=error` with severity ≥ threshold.
- **Reads:** the triggering error node; complication context slice (patient-twin snapshot, current vitals trend summary, current phase, recent errors sharing the same root cause); Literature Retrieval results via a synchronous inline call.
- **Writes:** complication candidate nodes (one or more per triggering error) with causal-reasoning edges to the error and evidence edges to retrieved literature nodes. Confidence per candidate. Reasoning trail as a node attribute.
- **Cache key:** `(error_category, current_phase, patient_twin_signature)` — a repeat within the same case reuses the prior result rather than re-firing.

No hand-authored error→complication table, ever. The query is the model's own, formulated from live context.

### 5. Literature Retrieval Agent

A single Europe PMC API call given a query string, result deduplication, per-case caching. Not an LLM agent — a tool wrapper, treated as an agent for graph-provenance and observability reasons.

- **Trigger:** called inline by #4; called inline by #6 if the replanner needs evidence beyond what the complication carries.
- **Reads:** a query string, a top-N parameter, the per-case retrieval cache.
- **Writes:** literature-evidence nodes for the top-N results; cache entries at `cases/{case_id}/retrieval_cache/{query_hash}`.
- **Rate:** bounded by cache — typically one real API call per unique query per case.

### 6. Corrective Replanning Agent

On any complication node: consume the triggering error, complication candidates with their literature, the full context slice, and the bounded corrective-action library. **Select from the library — never generate free-form clinical text.**

- **Trigger:** subscription on `node_type=complication`.
- **Reads:** triggering complication and its linked error; full replanning context slice (patient twin, vitals trend, current + recent phases from snapshot and event stream, currently active proposed trajectory if any); bounded corrective-action library JSON keyed by error category.
- **Writes:** corrective-trajectory nodes (dotted outline) with proposal edges to the triggering error and complication, including verification checks per action. May instead emit a "no confident match — escalate" node.
- **Cache key:** `(error_id, complication_slug)` — regenerates only when inputs change.

### 7. Trajectory Divergence Detection Agent

While a corrective proposal is active and not dismissed, continuously compare actual perception against the proposed trajectory.

- **Trigger:** activated by the presence of any active, non-dismissed corrective-trajectory node. Runs while ≥1 such proposal is active; deactivates when none remain.
- **Reads:** the active proposals; divergence context slice (active proposal + last N perception event nodes + current snapshot).
- **Comparison strategy:** **deterministic-first, LLM-fallback.** Simple structural checks (expected next gesture vs. observed gesture) run deterministically; semantic comparison ("is the surgeon following the spirit of the corrective path") escalates to a Gemini call. Both paths must be supported. Matching is on **semantic/text similarity, not exact string equality**.
- **Writes:** divergence-alert nodes with trajectory-comparison edges linking the actual perception window(s) to the proposed trajectory.
- **Rate:** polls every 5–10s while a proposal is active. Zero writes when aligned; one divergence-alert per confirmed divergence.

This is a **different signal** from an error detection. "An error happened" and "the actual trajectory departed from the safer plan we proposed" stay separate in both the node vocabulary and the schema — see [§16](#16-open-design-decisions) on naming.

### 8. Alert Routing Agent

On any divergence-alert node not silenced by HITL acknowledgment: assemble a structured alert payload with the reasoning chain and evidence citations, hand off to the Verification Gate before any external write.

- **Trigger:** subscription on `node_type=divergence_alert`, filtered by the HITL acknowledgment status of the underlying corrective proposal.
- **Reads:** the divergence-alert node, the underlying corrective-trajectory, the complication, the root error, and all linked evidence.
- **Writes:** an action-intent node representing the pending external write. Calls the Verification Gate synchronously.
- **Idempotency key:** `(case_id, divergence_alert_id)` — one alert per divergence, no re-fire.

### 9. Verification Gate Agent

Fail-closed reasoning gate over any proposed external write. **Read-only over the graph.** Checks that the reasoning chain has the expected structure, that provenance is traceable, that confidence thresholds are met, and that no links are missing.

- **Trigger:** called synchronously by Alert Routing and by the Documentation approval flow.
- **Reads:** the action-intent node and the full reasoning chain reachable via graph edges.
- **Writes:** verification-block nodes (visible on the graph) with structured pass-with-reason or block-with-reason outcomes.
- **On pass:** does *not* itself perform the external write — returns pass to the caller, which performs it.
- **On block:** no external write occurs, the block-reason is visible on the graph, the caller receives the block.
- **Never cached, never retried.** A stale pass is worse than a redundant verification, and a retried safety gate isn't a safety gate.

---

## 4. External write executors (thin adapters, not reasoning agents)

### Alert Executor

On verified pass from Alert Routing, posts to the configured external destination with the structured reasoning trail and evidence citations. Logs the delivery outcome back onto the graph as an action-outcome node.

> **Destination platform: open, and explicitly not Slack.** The earlier plan's Slack-webhook choice is retired — it demonstrates a generic webhook, not clinical integration. What's wanted is a healthcare-relevant destination that shows the same class of real external interaction that the FHIR write already does. Candidates need evaluating on: free/public sandbox access with no approval wait, a real API (not just a webhook), and clinical plausibility as an alert channel. Unresolved — see [§16](#16-open-design-decisions).

The adapter is written against a destination-agnostic interface (structured payload in, delivery outcome out) so the platform decision doesn't block the reasoning agents above it.

### FHIR Write Executor

On verified pass from the Documentation approval flow, writes a `DocumentReference` or `Composition` to the configured HAPI FHIR base URL, performs a readback verification, then logs the outcome node. `tools/fhir_write.py` carries over as-is; what's new is that the payload is graph-derived documentation.

---

## 5. Post-case agents

### 10. Benchmark Agent

- **Trigger:** case-close event.
- **Reads:** the full case graph (all error, anticipation, corrective, divergence nodes); SEDMamba ground truth via the strictly-separated validation-only tool (`tools/sedmamba_labels.py`); the anticipation accuracy sidecar log.
- **Does:** aligns predicted error nodes to ground-truth error events; applies the documented CARES-6 ↔ OCHRA-24 many-to-one mapping to aggregate SEDMamba onto the CARES-6 axis; computes per-category precision/recall/F1 and macro-F1; computes anticipation predicted-vs-actual next-phase accuracy.
- **Writes:** a benchmark node with the structured scorecard (per-category metrics + macro-F1 + anticipation accuracy + case-level counts).
- **Rate:** once per case at close.

This is the **one legitimate place ground truth enters the system** — post-hoc, never in a live decision path. The existing test that asserts `tools.sedmamba_labels` is absent from the live detection path's imports stays as the automatic guard.

### 11. Documentation Agent

- **Trigger:** case-close, **after** Benchmark completes (so the scorecard can be referenced in the note).
- **Reads:** the full case graph — perception snapshots, event stream, errors, complications, corrective proposals with acknowledgment status, divergences, alerts — plus the benchmark node.
- **Writes:** a documentation node with the drafted note: phases traversed, technique events with OCHRA grounding, complications reasoned, corrective proposals surfaced with acknowledgment outcomes, divergences alerted, benchmark summary, case summary.
- **No external write** until HITL approval + Verification Gate pass.
- **Rate:** once per case at close.

---

## 6. Root orchestrator responsibilities

One root orchestrator per case, wrapping everything above. Deliberately minimal, because ADK orchestration primitives handle most of the wiring.

1. On `POST /cases/open` from the frontend: mint a fresh `case_id`, resolve the video's real duration (`find_video_duration_s`), schedule the case as a FastAPI background task, return `case_id` immediately.
2. **Draw the static agent hierarchy on the graph before dispatching any sweep** — trigger node, all top-level agent nodes, Error Detection's role sub-agent nodes, all hierarchy edges. Sequentially. This exists to prevent the thread-pool-starvation regression (§7): cheap registration writes queued via `asyncio.to_thread` share Python's default thread pool with the Gemini SDK's blocking I/O, and once dozens of long-running calls are in flight a small write can be delayed by minutes. **Draw first, dispatch second.**
3. Dispatch the three long-running sweeps (Perception, Error Detection, Anticipation) concurrently via `asyncio.gather`. These are the only three that run for the duration.
4. Register event-driven agent subscriptions against the graph-change bus (§6).
5. On case-close (video end or explicit close event): stop the sweeps, wait for in-flight work to drain (bounded ~30s), then run Benchmark, then Documentation, then await HITL approval before completing.

> Step 5's "await HITL approval" **cannot be a literal in-memory `await` that blocks indefinitely** — a background task parked forever doesn't survive Cloud Run scale-to-zero or an instance recycle, and the approval may arrive hours later. The `awaiting_approval` state must be durable in Firestore so a later HTTP call resumes the flow, rather than a coroutine holding a process open. Flagged in [§16](#16-open-design-decisions).

---

## 7. The graph-change event bus

Event-driven agents must not poll the graph — polling wastes calls and misses fast events.

- The shared state service already emits SSE diffs on `GET /stream` for the frontend. Extend this: the same diff stream is subscribable from Python backend agents as an **in-process pub/sub feed derived from the same `apply_state_patch` write path**.
- Each event-driven agent registers a filter (e.g. "trigger on new nodes where `node_type=error` and `severity >= medium`"). On a matching write, the agent's handler is invoked with the new node.
- Handlers run as `asyncio` tasks — one task per triggering event. Task lifecycle is tracked by the orchestrator so case-close can drain.
- The subscription bus is **per-case, keyed by `case_id`**. An orchestrator instance owns the case-scoped bus.

**Why in-process rather than a Firestore listener:** latency (sub-ms vs. hundreds of ms) and dependency simplification (avoids a second consumer of Firestore's real-time listener contract). Firestore remains the durable store and the SSE-to-frontend source; the backend agent bus is a *sibling subscriber to the same write path*, not a Firestore listener.

**The invariant this depends on, stated explicitly because it's easy to violate later:** all of a case's agents run inside the one process that owns that case (the orchestrator's background task). The bus taps the **caller side** of `apply_state_patch` — inside `orchestrator_service`, where agents actually execute — not the store side inside `state_service`, which is a separate process. If a case's agents were ever split across processes or instances, the in-process bus would silently miss cross-process writes. The original plan's multi-tenancy design already guarantees one case = one orchestrator background task, so this holds today; it's a real constraint on any future distribution of agent work.

---

## 8. Concurrency model

Three levels, each with its own discipline.

**Level 1 — sweep parallelism.** Perception, Error Detection, and Anticipation run as three top-level `asyncio.gather`-dispatched coroutines from the orchestrator. No direct shared state; they share only the graph via `apply_state_patch`.

**Level 2 — within-sweep parallelism.** Error Detection's screen and deep passes run their role × category calls in a nested `asyncio.gather` under the sweep. Perception is single-call-per-window internally. Anticipation is single-call plus a bounded-time background reconciliation coroutine.

**Level 3 — event-driven agent parallelism.** Each handler runs as its own `asyncio.create_task`. Multiple can run concurrently for different triggering events, and a single agent can have multiple in-flight tasks. The orchestrator maintains a `set[asyncio.Task]` per case for lifecycle tracking and drain-on-close.

### The known pitfall: thread-pool starvation

`asyncio.to_thread` uses Python's **default** thread pool executor, shared with whatever blocking I/O the Gemini SDK does underneath. Once dozens of concurrent Gemini calls saturate the pool, unrelated `apply_state_patch` writes queued via `to_thread` can be delayed by minutes. This is not hypothetical — it was diagnosed from real Firestore document timestamps and is the reason `_draw_static_hierarchy` exists.

Mitigations, in order:

1. Draw all static hierarchy nodes/edges before any sweep starts. *(Already implemented.)*
2. **A dedicated bounded `ThreadPoolExecutor` for state-service writes**, so registration/heartbeat/event writes cannot be starved by Gemini I/O. Currently a "consider"; it becomes **non-negotiable at the first observed regression** — and the event-driven downstream agents add real new concurrent write pressure, so expect to need it.
3. Make the write path async-first rather than `to_thread`-wrapped. Concretely, today `tools/state_tools.py` wraps a blocking `requests.post` in `asyncio.to_thread`; the real fix is swapping to `httpx.AsyncClient` so the HTTP call never touches the thread pool at all.

---

## 9. Idempotency, caching, and retry

Every agent must be safe to invoke twice on the same trigger — the event bus may deliver duplicates, and retries after transient failures are expected.

| Agent | Idempotency key |
|---|---|
| Perception | `(case_id, window_index)` — second invocation returns cached result, no re-fire |
| Error Detection | `(case_id, window_index)` |
| Anticipation | `(case_id, prediction_id)` — slug of free-text prediction + current phase |
| Complication Reasoning | `(case_id, error_category, current_phase, patient_twin_signature)` |
| Literature Retrieval | `(case_id, query_hash)` — Firestore-backed per-case cache |
| Corrective Replanning | `(case_id, error_id, complication_slug)` |
| Divergence Detection | none needed *(see caveat below)* |
| Alert Routing | `(case_id, divergence_alert_id)` |
| Verification Gate | **no cache** — always re-verifies |
| Benchmark, Documentation | `(case_id,)` — second call returns cached |

> **Caveat on Divergence Detection:** the source spec justifies "no idempotency needed" with "deterministic given input state" — but the same agent is specified to fall back to a Gemini call for semantic comparison, which is *not* deterministic. The deterministic-first path needs no key; the LLM-fallback path does. Flagged in [§16](#16-open-design-decisions).

**Retry policy:**

- **Gemini call failures:** 3 retries, exponential backoff (1s → 3s → 9s), then emit a degraded-outcome node and continue. Never block a sweep on a single failure.
- **Europe PMC failures:** 2 retries, then empty result; Complication Reasoning proceeds without citations and sets `evidence_unavailable` on the complication node.
- **State service write failures:** retry with backoff; if the state service is down >30s the sweep raises and the case **fails open** — the orchestrator surfaces the failure to the UI rather than silently continuing.
- **Verification Gate:** never retried. It's the safety layer.

---

## 10. Failure and degradation modes

The system degrades gracefully; downstream agents are never blocked by upstream failures.

| Failure | Behavior |
|---|---|
| Perception call fails/times out | No events for that window. Downstream sees **silence, not error**. Snapshot slots retain previous values. |
| Error Detection call fails | No error nodes for that window; Complication Reasoning simply doesn't trigger. |
| Anticipation call fails | No prediction that cycle; next cycle proceeds normally. |
| Complication Reasoning fails | Emits a degraded-complication node with an explicit `reasoning_unavailable` flag. Corrective Replanning does **not** trigger on degraded nodes. |
| Literature Retrieval fails | Complication Reasoning proceeds with `evidence_unavailable=true`. Per Verification Gate policy, alerts based on evidence-less complications **must be blocked**. |
| Verification Gate blocks | External write suppressed, block-reason visible on the graph, no retry. |
| State service unreachable | Orchestrator fails the case open-with-error; all in-flight work cancelled; UI surfaces the failure. |

**Every degradation writes a structured event onto the graph or the agent's own node attrs**, so the failure is visible and auditable rather than silently swallowed. This is the same standing rule as [[feedback_no_fake_fallback_data]]: a failure state must look like a failure, never like a quieter version of success.

---

## 11. HITL surfaces

Two surfaces, primarily frontend concerns, each triggering a backend state transition the orchestrator must handle. **`POST /events/manual` on the state service is the sole channel for HITL events into the system** — neither surface gets its own backend endpoint.

### HITL #1 — Advisory acknowledgment (in-loop, during case)

When a corrective-trajectory node is written, the frontend renders it with an acknowledge/dismiss control. On click, the frontend posts a structured `hitl_acknowledgment` event carrying the corrective-trajectory node ID and outcome (`acknowledged` | `dismissed`). The orchestrator subscribes to this event kind and updates `acknowledged_at` / `acknowledgment_outcome` on the corrective-trajectory node. Alert Routing's filter checks this attribute — **acknowledged proposals do not fire alerts on divergence; they emit silent-log events instead.**

### HITL #2 — Documentation approval (post-case, out-of-loop)

After Documentation completes, the orchestrator enters `awaiting_approval`. The frontend renders the draft with approve/edit/reject controls.

- **Approve** → orchestrator calls the Verification Gate on the (possibly edited) documentation node → on pass, calls the FHIR Write Executor.
- **Reject** → case closes without a FHIR write.
- **Edit** → orchestrator updates the documentation node and re-enters awaiting-approval.

> `POST /events/manual` today accepts only `{case_id, text}` and always writes a `node_type="event"`, `source_agent="human"` node — it has no notion of a structured event *kind*, a target node ID, or an outcome enum. Carrying HITL acknowledgments and documentation approvals through it requires a real extension of its request schema, not just new callers. Flagged in [§16](#16-open-design-decisions).

---

## 12. Graph-write discipline and ID conventions

**Every agent, without exception, touches the graph only through `tools/state_tools.py::apply_state_patch(case_id, node=..., edge=..., ...)`.** No direct Firestore writes from agent code. No SSE injection from agent code. This single-write-path discipline is what makes the change bus reliable and the audit log complete.

Node/edge ID conventions are enforced **by convention only** — the store has no foreign-key enforcement:

| Kind | Convention |
|---|---|
| Agents | `agent:{name}` |
| Phase snapshot | `snapshot:current_phase` (+ `phase:{opaque_id}:{window_index}` for per-window evidence if needed) |
| Entities | `entity:{stable_id}` — stable across windows, assigned by Perception |
| Perception events | `event:{monotonic_seq}:{event_kind}` — strictly monotonic per case |
| Errors | `error:{window_index}:{category}` |
| Anticipation predictions | `predicted-phase:{slug}` |
| Complications | `complication:{root_error_id}:{slug}` |
| Corrective trajectories | `corrective:{root_error_id}:{slug}` |
| Divergence alerts | `divergence:{proposal_id}:{window_index}` |
| Verification blocks | `verification:{action_intent_id}` |
| Literature evidence | `literature:{query_hash}:{result_index}` |
| Action intents | `action_intent:{kind}:{ulid}` |
| Benchmark | `benchmark:{case_id}` — singleton per case |
| Documentation | `documentation:{case_id}` — singleton per case |

Whoever writes an edge references a `node_id` someone else has used or will use later. **Convention violations silently orphan edges** — a periodic reconciliation check should flag orphaned edges onto the case's own graph as visible warnings. (The frontend half of this already bit once: ReactFlow silently drops an edge whose endpoint node is missing, with no error. The backend has no equivalent guard at all.)

---

## 13. Case lifecycle, end to end

1. **Case open** — orchestrator mints `case_id`, draws static hierarchy, dispatches three sweeps concurrently, registers event-driven subscriptions.
2. **Steady state** — Perception, Error Detection, Anticipation run continuously. Event-driven agents fire on qualifying graph changes. HITL #1 acknowledgments arrive occasionally and update corrective-trajectory attrs. Divergence Detection runs whenever any active proposal exists. Alert Routing → Verification Gate → Alert Executor fires on unacknowledged divergences (destination platform TBD, §4).
3. **Case close** — video ends or explicit close. Orchestrator stops sweeps, drains in-flight event-driven tasks (bounded 30s, then cancels).
4. **Benchmark** — Benchmark Agent runs. Scorecard renders on UI.
5. **Documentation draft** — Documentation Agent runs. Draft renders in the review panel.
6. **HITL #2** — surgeon approves/edits/rejects. Approve → Verification Gate → FHIR Write Executor → readback → outcome node. Reject → case ends.
7. **Case sealed** — orchestrator marks the case complete, all agent nodes marked terminal. The graph is now the immutable case record.

---

## 14. Observability

Every agent invocation must be traceable end-to-end, for the demo and for the architectural-discipline scoring axis.

- **OpenTelemetry span per agent invocation.** Root span at case open; each sweep a child span; each event-driven invocation a child span linked by triggering-node ID; each Gemini call a child span within the invocation. Spans carry the triggering node ID and resulting node IDs as attributes.
- **Graph-side provenance.** Every node's `source_agent` and `source_tool` must be populated (already enforced by `Provenanced` in `state/schema.py`). Every reasoning-produced node should additionally carry a `trace_id` linking to its span, so a UI click can jump from a graph node to its full reasoning trace.
- **State transitions logged as events:** case open, sweeps dispatched, case close, drain complete, benchmark start, documentation start, HITL awaited, HITL received, external write attempted, external write outcome — each a structured log line with `case_id` and span context.

GEAP's observability stack can be explored for this later; **not a priority now.**

---

## 15. Existing assets — reuse, adapt, retire

The current codebase is a parts bin for this plan, not a baseline it has to respect. Inventoried against the real repo as of 2026-08-15, sorted by how each piece lands here.

**Reuse as-is** — fits this plan unchanged:

| Asset | Where |
|---|---|
| Single-write-path discipline (`apply_state_patch`, already `async def`) | `tools/state_tools.py:69` |
| Static-hierarchy-before-dispatch, the thread-pool-starvation fix | `agents/orchestrator/agent.py::_draw_static_hierarchy` |
| Concurrent sweep dispatch via `asyncio.gather` | `agents/orchestrator/agent.py:201` |
| Fresh-`case_id`-per-open, per-case Firestore isolation | `services/orchestrator_service/main.py:64` |
| SSE diff stream to the frontend | `GET /state/{case_id}/stream` |
| `Provenanced` on every node/edge | `state/schema.py` |
| Video/frame utilities (duration, fps, window generation, sampling, multimodal content) | `tools/video_utils.py` |
| FHIR write + readback verification | `tools/fhir_write.py` |
| Europe PMC retrieval | `tools/europepmc_rag.py` |
| Ground-truth strictly walled into a validation sidecar, guarded by an import-inspection test | `tools/sedmamba_labels.py` + `tests/` |

**Adapt** — the core is right, the shape changes:

| Asset | Change needed |
|---|---|
| Scene Graph Builder → **Perception Sweep** | Reasoning call largely survives; the entire change-diff / debounce / rate-ceiling / snapshot-slot / audit-log layer is new around it |
| Monitor → **Error Detection Sweep** | Cascade mechanics unchanged; emits onto the new error node kind and becomes a real event source for downstream agents |
| Anticipation → **Anticipation Sweep** | Mechanics survive; cadence and its relationship to the shared window grid to be settled |
| `state/schema.py` node/edge vocabulary | Real expansion to the v2 kinds — a migration, not additive-only |
| `POST /events/manual` | Needs structured `event_kind` + `target_node_id` + `outcome` to carry both HITL surfaces |
| `tools/state_tools.py` transport | `requests.post` in `asyncio.to_thread` → `httpx.AsyncClient`, plus a dedicated bounded executor for any remaining thread-offloaded writes |
| Frontend graph palette + legend | Node-kind coloring alongside agent-identity coloring; legend rebuilt to match |
| `RetrievalPanel` / `ActionLogPanel` | Restructured per `plan_v2` §8 |

**Build** — nothing exists: the in-process graph-change bus, agents 4–11, both executors, the patient twin, the vitals stream, the bounded corrective-action library, the context-slice assembler, the perception audit-log subcollection, the orphaned-edge reconciliation check, OpenTelemetry instrumentation.

**Retire** — the rescope made these irrelevant: the Slack-webhook alert destination (§4), `TrajectoryPatch`'s unused `candidate_plan`/`recovery_option` kinds if Corrective Replanning's proposal shape supersedes them, and fixed-width label truncation in the graph (replaced by variable-length nodes with auto-reflow).

Manual offline validation scripts (`scripts/summarize_monitor_accuracy.py`, `scripts/summarize_anticipation_accuracy.py`) sit between reuse and adapt: their scoring logic is real and correct, but under this plan the Benchmark Agent runs per-case and writes a graph node, rather than a human running a script over a corpus-wide log. The scoring functions get lifted; the CLI wrappers stay for offline use.

---

## 16. Open design decisions

Decisions that need making before coding, not during.

1. **Alert Executor destination platform** — open, explicitly not Slack. See §4 for the evaluation criteria.
2. **Node-type vocabulary for `event`.** Three distinct things want this name: Monitor's alarm-styled divergence detections, manually-injected human events, and the new high-volume perception event stream. They need distinct types and distinct visual treatment — a high-frequency "entity appeared" must not render like an alarm.
3. **Divergence naming.** "An error was detected" and "the actual trajectory departed from the proposed corrective plan" are different signals. The node vocabulary already separates them (`Error` vs. `Divergence-alert`); the schema needs to as well, rather than one overloaded model.
4. **Who generates the perception event `monotonic_seq`.** The state service already owns a transactional monotonic `seq` per case, but reusing it couples node *identity* to write *ordering* and breaks on retry. A separate per-case perception counter is likely right — decide deliberately.
5. **Error severity.** Complication Reasoning triggers on "error node above severity threshold." Error nodes carry `confidence` and `composite_score` — severity needs to be defined and stored, or the trigger expressed in terms of what actually exists.
6. **Divergence Detection idempotency.** No key is needed for the deterministic path; the LLM-fallback path needs one.
7. **`awaiting_approval` durability.** A background task parked on an in-memory `await` won't survive scale-to-zero or an instance recycle, and approval may arrive hours later. Needs durable state with a resume-on-HTTP-call path.
8. **Anticipation cadence** — its own cycle, or the shared 5s window grid.
9. **Color axis coexistence** — node-kind coloring vs. agent-identity coloring on the same node (fill vs. border, or icon vs. fill).
10. **Where the benchmark scorecard and documentation draft render** — modal, dedicated post-case view, or a panel.
11. **The bounded corrective-action library** is the highest-risk hand-authored artifact in this plan — clinical content with a real safety surface. Its provenance-tier tagging and its "no confident match — escalate" exit are what keep it honest; both need to exist in the first version, not be retrofitted.
