# SurgGraph v2 — Autonomous Surgical Safety System

**Status: this is the plan.** A rescope decided 2026-08-15 — not a delta on top of the existing build. This doc and `docs/agentic_workflow.md` together define the target system; the existing codebase is an asset pool to draw from, not a baseline to preserve. Reuse what fits, adapt what nearly fits, build what's missing, retire what the rescope made irrelevant ([§9](#9-existing-assets--reuse-adapt-retire) inventories which is which).

**Companion:** `docs/agentic_workflow.md` is the implementation reference for **how** the agents are wired — the agent roster, triggers, the graph-change event bus, concurrency levels, idempotency/retry/failure discipline, ID conventions, and observability. This doc is the **what**. Read both; where they disagree about agent wiring, `agentic_workflow.md` wins.

Older docs are historical record, not constraints: `docs/HANDOVER.md` (what the pre-rescope build was), `docs/validation_results.md` (real measured accuracy, still valid for the components that survive), `docs/latency_optimization.md`, and `~/.claude/plans/consider-the-initial-11082026-md-harmonic-cloud.md` (why the earlier design decisions were made — the *principles* there still bind, the *scope* does not).

---

## 1. What SurgGraph is (v2 framing)

SurgGraph is an autonomous surgical safety system for robot-assisted radical prostatectomy (RARP). It watches a case in real time, catches OCHRA-grounded technique errors, reasons over patient-specific complication risk using published literature, proposes a corrective near-term trajectory to minimize that risk, and alerts when the actual trajectory diverges from the safer path. At case end, it self-benchmarks its predictions against SEDMamba ground-truth annotations and drafts a surgeon-approved operative record as a byproduct — closing the loop from perception through reasoning through action through evaluation.

**Two value axes, weighted equally:**

1. **Patient safety** — surface errors, reason about downstream complications, and propose safer trajectories with divergence alerting.
2. **Documentation burden reduction** — the reasoning graph already contains the operative narrative; the system generates intraop report/documentation using that graph, as a byproduct of reasoning that already happened rather than a separate authoring pass.

The spine that delivers both: error → complication hypothesis → literature grounding → corrective proposal → divergence-vs-proposal detection → HITL → fail-closed verification → external write (alert + FHIR) → post-case grading + documentation draft. The pre-rescope build reached only the first link of that chain (detection, forecasting, scene description, feeding a mostly-descriptive graph); everything downstream of an error node is the new work.

---

## 2. Synthetic data (clearly labeled as synthetic, everywhere it surfaces)

- **Patient twin profile**: plausible RARP-relevant fields — ASA class, BMI, prostate volume, nerve-sparing plan, comorbidity flags. Loaded once per case, referenced by downstream reasoning as prior context (part of every relevant context slice, §5).
- **Vitals stream**: HR, MAP, SpO2, EtCO2, airway pressure trend. Synthetic timeline replayed against the video clock with plausible pneumoperitoneum/Trendelenburg-consistent trends and a couple of scripted excursions, specifically to exercise the complication-reasoning path (an excursion should be able to trigger real reasoning, not just decorate a panel).

Per the standing project rule ([[feedback_no_fake_fallback_data]] in memory — never disguise fabricated data as a real signal), synthetic data here is architecturally load-bearing (it's what makes complication reasoning and divergence-alert demos possible without live OR telemetry) but must be **visually and textually labeled synthetic wherever it appears on the UI**, not presented as if it were a real monitor feed. This is a different case from "fake fallback on backend failure" — it's disclosed synthetic input data, not a masked failure — but the disclosure obligation is the same spirit: never let a viewer mistake it for something it isn't.

## 3. External retrieval

- **Europe PMC REST API** (free, keyless) for on-demand literature retrieval when reasoning about complications — same tool already scoped in the original plan's §3 (`search_literature`), now invoked live/on-demand per complication-reasoning event rather than only pre-cached. Pre-caching may still be used to seed a corpus, but the query itself continues to be agent-formulated at call time, never a static template (unchanged rule from the original plan's §3).

---

## 4. Foundational state layer: the Living Graph

A single shared graph is the system's ground truth for what has happened, what is happening, what is predicted to happen, and what is proposed to happen. Every reasoning step reads from it and writes to it.

### 4.1 Node kinds

| Node kind | Contents | Color |
|---|---|---|
| Perception | entities, anatomy regions, instruments, activity descriptions | default |
| Phase/step (current, historical) | current + historical procedure phase | default |
| Error | one per detected event | orange |
| Complication | reasoned candidates with confidence + evidence pointers | orange |
| Corrective-trajectory | normative proposals for what should happen to minimize a specific complication | **dotted-outline**, yellow |
| Divergence-alert | actual trajectory diverges from a proposed corrective trajectory beyond threshold | red |
| Literature-evidence | retrieved citations attached to complication or corrective-trajectory nodes | blue |
| Verification-block | fail-closed gate outcomes when external writes are proposed | green |
| Benchmark | post-case predicted-vs-actual comparison per category | brown |
| Documentation | post-case operative note draft, pending HITL approval | brown |

Node sizing: standard/fixed height (the dimension perpendicular to graph flow), length grows with label text along the flow direction; neighboring nodes auto-adjust position so growing a node never causes overlap. This replaces the current fixed-width-plus-truncation approach, which means the layout math has to change with it — dagre's `ranksep`/`nodesep` tuning currently assumes a fixed box size, and per-node measured width has to feed the layout pass instead. The crowding problem truncation was solving is real, so auto-reflow has to actually solve it too: variable-length nodes need enough separation that a long label doesn't collide with its neighbors' rank.

### 4.2 Edge kinds

| Edge kind | Meaning | Style |
|---|---|---|
| Detection | perception → error/divergence | solid |
| Causal-reasoning | error → complication | solid |
| Evidence | literature → complication or corrective-trajectory | solid |
| Prediction | current state → future trajectory state | dashed |
| Proposal | error+complication → corrective-trajectory | dashed |
| Trajectory-comparison | actual → corrective-trajectory, carries alignment/divergence signal | solid |
| Confirmation | predicted node reconciled against realized state | solid |
| Verification | proposed action → verification-block outcome | solid |
| Grading | predicted → ground truth, post-case only | solid |

### 4.3 Rules

- Every write goes through a single shared state-mutation API. No direct backing-store access from reasoning steps.
- Node/edge linking has no foreign-key enforcement; correctness depends on all writers agreeing on the same node-ID convention:
  `phase:{opaque_id}`, `entity:{stable_id}`, `error:{window}:{category}`, `complication:{root_error_id}:{slug}`, `corrective:{root_error_id}:{slug}`, `divergence:{proposal_id}:{window}`.
- Every node carries provenance metadata: which reasoning step produced it, which tool call generated it, timestamp, and confidence where applicable.
- The graph is domain-agnostic at the storage layer — the shared state service knows nothing about surgery, only about generic multi-tenant graph storage and change streaming.
- **Every node is tied to and orderable by timestamp.** All node kinds carry a timestamp (already true of `Provenanced` in `state/schema.py`), and the graph must support genuine chronological ordering/replay across *all* node kinds together, not just within one kind — relevant to the event-stream design in §7, and needing a real frontend affordance (timeline scrub or chronological list view), not just an internal sort key.

The ground-truth rule carries forward and binds every reasoning step below: opaque/structural signals are legitimate input context; semantically-named/labeled ground truth is validation-sidecar only, never live-decision input. Most directly — complication reasoning must not be handed the SEDMamba error label, corrective replanning must not be handed the "correct" answer, and self-benchmarking is the one legitimate place ground truth enters, post-hoc only.

## 5. The graph-at-instant context slice pattern

The single most important architectural pattern in this system, and a generalization of the standing principle that state is queried via tools, never carried in prompt context.

At each reasoning call, in addition to the current frame window and any structural signal, the caller receives a JSON snapshot of the relevant subgraph — a materialized **context slice** — assembled deterministically from the current graph state before the call is dispatched. Each reasoning role gets a different slice shape tuned to its needs:

| Reasoning role | Context slice contents |
|---|---|
| Perception | recently observed entities + previous window's activity description (temporal continuity) |
| Error detection | recent error history + current phase |
| Complication reasoning | triggering error, patient twin, recent vitals trend, current phase |
| Corrective replanning | full state: triggering error, complication candidates with literature, patient twin, vitals trend, current + recent phases, current active proposed trajectory (if any) |
| Divergence detection | currently active proposed trajectory + last N windows of actual perception |
| Documentation (case end) | entire case graph |

This gives every call genuine temporal/state awareness without requiring any reasoning step to walk the graph itself. The existing `get_state_snapshot`/`get_recent_window_state` tools already do a generic version of this for Anticipation; the change here is that assembly becomes deterministic and per-role, tuned to what each caller actually needs, rather than one shared snapshot shape.

---

## 6. End-to-end workflow, in order

Renumbered sequentially from the original draft for clarity (no content dropped or reordered) — original had a numbering gap between the benchmarking sub-step and documentation drafting; here it's a clean 1–14.

### 1. Case open (trigger)

An HTTP request or first-frame-play event opens a case. A fresh `case_id` is minted. The case's static structural elements are drawn on the graph up front — hierarchy of reasoning roles, trigger node, patient-twin node populated from the illustrative profile — before any sweep starts. This up-front draw exists to avoid thread-pool starvation where cheap registration writes get queued behind long-running Gemini calls once concurrent sweeps are in flight. The mechanism carries over from the existing `_draw_static_hierarchy`, extended to cover the full agent roster and the patient-twin node.

### 2. Perception pipeline (fast loop, per ~5s window)

Video decoded into a ring buffer, sampled at low framerate (5–10 fps). Each sliding window (~5s):

- Perception step describes the entities, anatomy regions, instruments, and activity in the current window, using stills at native resolution plus its context slice.
- Perception step registers/updates entity nodes with stable IDs, relation edges, and the current phase node's semantic label.
- Vitals synthetic stream is polled for the window; if a trend flag or excursion is present, a physiological-state node is written.
- Change-diff triggers downstream steps only on meaningful state change (new gesture, phase transition, new error signature, vitals excursion) rather than every window uniformly.

The full two-tier data structure and emission-cadence rules for this step are specified in [§7](#7-perception-data-structure-and-emission-cadence) — this is the most detailed and most novel part of the rescoping, so it gets its own section rather than being folded in here.

### 3. Error detection (parallel to perception)

The existing Monitor cascade carries over largely unchanged in mechanics — restated here for workflow completeness:

- Cascade design, both tiers on still frames: screen pass (temporal/spatial/procedural role-specialist calls) + deep pass (role × category matrix, unconditional, parallel).
- Deterministic weighted aggregation over the concurrent role outputs; requires ≥2-of-3 role agreement above threshold to fire an error event.
- Each independent above-threshold category emits its own error node — a window can produce zero, one, or several error nodes.
- Categories: the 6 CARES-collapsed OCHRA groupings (Multiple Attempts, Out of View, Needle Handling, Tissue Handling, Suture Handling, Instrument Control), each OCHRA-grounded in the reasoning prompt.
- Error nodes never overwrite a semantic phase label written by perception; error detection has no general activity description of its own to offer.

### 4. Complication reasoning (event-driven, on error node) — NEW

When an error node fires above severity threshold:

- Reasoning step consumes the triggering error, its context slice (patient twin, current vitals trend, phase), and Gemini's general medical knowledge.
- Formulates its own literature query — **no hand-authored error-to-complication table**, ever. The query is the model's own, formulated from live context.
- Literature retrieval fires a Europe PMC API call with that query, returns top-N results, caches per case.
- Reasoning step emits complication candidate nodes (one or more), each with confidence, a causal-reasoning edge to the triggering error, and evidence edges to the retrieved literature nodes.

### 5. Corrective trajectory replanning (event-driven, on complication node) — NEW

When a complication node is written:

- Replanning step consumes the triggering error, the complication candidates with their literature evidence, the full context slice, and a bounded corrective action library keyed by error category.
- The bounded action library is a JSON file, pre-authored, tier-2-sourced (derived from OCHRA error descriptions, since the corrective action is often the inverse of the deviation — each entry tagged with provenance tier, matching this project's tiered-honesty disclosure pattern for hand-authored content). This is the highest-risk hand-authored artifact in the plan; its provenance tagging and escalate-exit are what keep it honest.
- Replanning step selects from the bounded library — **never generates free-form clinical text**. Output is a structured proposal: a short sequence of corrective actions with verification checks per action, plus a "no confident match — escalate" exit.
- Emits corrective-trajectory nodes as dotted-outline yellow nodes on the graph, linked to the triggering error and complication with proposal edges.

### 6. Trajectory divergence detection (continuous, once a proposal is active)

When a corrective proposal is active, a divergence-detection step polls the actual perception stream against the proposed trajectory:

- Consumes its context slice (active proposal + last N windows of actual perception) at each check.
- If actual and proposed align within tolerance, no action.
- If actual diverges beyond threshold, emits a divergence-alert node linked by a trajectory-comparison edge.

This is measured against a **normative** proposal (what should happen), not a **predictive** forecast (what was expected to happen). Those are different signals and stay separate in both the node vocabulary and the schema — see [§10](#10-open-decisions) on naming.

### 7. HITL surface #1 (advisory, in-loop) — NEW

- When a corrective proposal fires, the UI surfaces it with a one-tap acknowledge/dismiss.
- If acknowledged: divergence detection runs in advisory-silent mode (still logs, no alert).
- If not acknowledged and actual diverges: alert path fires (step 9).
- This is the tiered-autonomy CDS pattern: autonomous by default, escalates on divergence, respects surgeon judgment when engaged.

### 8. Alert routing (on unacknowledged divergence)

- Alert-routing step consumes the divergence-alert node, the underlying complication, and the reasoning trail.
- Assembles a structured alert payload with the reasoning chain and evidence citations.
- Hands to the verification gate (step 9) before any external write.

### 9. Verification gate (fail-closed, on any proposed external write)

- Fail-closed by default: any external write (alert, FHIR write) must pass verification.
- Verification step is read-only over the graph — checks the reasoning chain has expected structure (error → complication with evidence → proposed action → traceable provenance), confidence thresholds met, no missing links.
- Emits a verification-block node with structured outcome (pass or block-with-reason). Blocks are visible on the graph.
- On pass, the external write proceeds. On block, the alert is suppressed and the block-reason is visible.

Fail-closed verification was a non-negotiable in the earlier plan and was never built; here it gets a concrete graph-visible node kind (`Verification-block`, green) and two real callers.

### 10. External action — alerting

- On verified pass, autonomous post to an external destination (env-provided config) with the structured reasoning trail, divergence details, and evidence citations.
- **Destination platform is open, and explicitly not Slack.** A generic chat webhook demonstrates plumbing, not clinical integration — what's wanted is a healthcare-relevant destination showing the same class of real external interaction the FHIR write already does. Evaluation criteria: free/public sandbox with no approval wait, a real API rather than just a webhook, and clinical plausibility as an alert channel. See [§10](#10-open-decisions).
- The post itself is logged back onto the graph as an action node with an outcome edge.
- The adapter is written against a destination-agnostic interface (structured payload in, delivery outcome out) so this decision doesn't block the reasoning agents above it.

### 11. Case close (trigger) → self-benchmarking (post-case, autonomous)

Either the video ends or an explicit case-close event fires. The fast loop stops. Two post-case steps run — self-benchmarking here, documentation drafting in step 12.

- Benchmark step reads the full case graph.
- Reads SEDMamba ground-truth error labels from the validation sidecar — **this is the one legitimate place ground truth enters the system, post-hoc, never in the live decision path** (unchanged rule from the original plan's §12).
- Aligns predicted error nodes (across time and category) to ground-truth error events.
- Because Monitor runs on 6 CARES-collapsed categories while SEDMamba ground truth is in 24-code OCHRA space, apply a documented many-to-one mapping (aggregate SEDMamba to the CARES-6 axis) and benchmark on the CARES-6 level — explicitly documented as a taxonomy alignment decision.
- Computes per-category precision/recall/F1 and overall macro-F1.
- Writes a benchmark node to the graph with the scorecard; UI renders it as a case scorecard panel.
- Also computes anticipation predicted-vs-actual next-phase accuracy from the anticipation-accuracy validation sidecar.
- This step is what actualizes the "self-evolving/closed loop" story: the case has graded its own reasoning against ground truth by the time it's done.

The scoring logic exists already as offline scripts over corpus-wide logs (`scripts/summarize_monitor_accuracy.py`, `scripts/summarize_anticipation_accuracy.py`, and the `data/validation/*.jsonl` they read). Here it becomes a live per-case step that reads one case's own graph and writes a `Benchmark` node — the scoring functions lift over; the CLI wrappers stay for offline use.

### 12. Documentation drafting (post-case, HITL-gated)

- Documentation step reads the full case graph — all perception, errors, complications, corrective proposals, divergences, alerts, benchmark.
- Drafts a structured operative note: phases traversed, notable technique events with OCHRA grounding, complication considerations reasoned, corrective proposals surfaced (and whether acknowledged), divergences alerted, case summary.
- Emits a Documentation node to the graph with the draft.
- UI renders the draft in a review panel with an approve button.

### 13. HITL surface #2 (approval, out-of-loop)

- Surgeon reviews the drafted note in the UI.
- One-tap approve/edit/reject.
- On approve, the note goes to the verification gate (step 9).

### 14. External action — clinical write

- On verified pass, FHIR write to the configured HAPI FHIR server as a `DocumentReference` or `Composition` resource.
- Readback verification (fetch what was written, compare).
- Result logged back to the graph as an action node with outcome edge.
- Case is now closed end-to-end.

`tools/fhir_write.py` (readback-verified writes against the public HAPI test server) carries over as-is; what's new is that the content being written is a graph-derived documentation draft rather than a placeholder.

---

## 7. Perception data structure and emission cadence

This is the piece of the rescoping with the most new mechanical detail, so it's kept as its own section rather than folded into step 2 above.

### 7.1 The mental model

Think of the graph as having two overlaid views on the same nodes:

- **Snapshot view** — "what is true right now": a small, bounded set of currently-active entities and one current phase. Roughly O(10) nodes at any moment, regardless of case length.
- **Event stream view** — "what has happened": append-only, monotonic, one node per meaningful change. Grows linearly with meaningful events, not with time.

Perception's job is to update the snapshot silently (in-place mutation on existing nodes) and emit an event only when something changed enough to matter. If nothing meaningful happened in a window, perception emits nothing. **Steady-state should be silent.**

### 7.2 Tier 1 — Entity registry (long-lived, in-place-updated)

One node per distinct real-world thing perceived in this case:

```
node_id: entity:{stable_id}                # e.g., entity:instrument_needle_driver_left
node_type: entity
label: "Needle Driver (left arm)"
attrs: {
  kind: "instrument" | "anatomy" | "material",
  first_seen_window: 42,
  last_seen_window: 187,
  is_active: true,                          # in the current window
  observation_count: 145,                   # total windows observed in
  confidence_rolling: 0.91                  # smoothed over recent windows
}
source_agent: "perception"
timestamp: {last_updated_at}
```

Rules:

- **Idempotent**: perception looks up the entity by `stable_id` first; if present, updates `attrs` in place (last-write-wins is fine here). If absent, creates.
- `is_active` flips as the entity enters/leaves the visible scene, but the node itself persists — instruments aren't deleted when they leave frame, just marked inactive. This preserves the entity's identity across occlusions.
- Stable IDs must be canonical strings the perception step commits to (`instrument_needle_driver_left`, not a fresh UUID per window). This is a structural rule, not an implementation detail — the whole entity registry collapses into per-window duplicates without it.

### 7.3 Tier 2 — Event stream (append-only, monotonic)

One node per meaningful thing that happened. Events are immutable once written.

```
node_id: event:{monotonic_seq}:{event_kind}
node_type: event
label: "Needle Driver picked up needle"     # human-readable, one line
attrs: {
  event_kind: "entity_appeared" | "entity_disappeared"
            | "relation_started" | "relation_ended"
            | "activity_changed" | "phase_changed"
            | "state_summary",
  window_index: 187,
  video_time_s: 934.5,
  involved_entity_ids: ["entity:instrument_needle_driver_left", "entity:material_suture_1"],
  detail: {...event-kind-specific structured payload...}
}
source_agent: "perception"
timestamp: {when the reasoning call finished}
```

Events link to entity nodes via `involved` edges, not by copying entity data into the event payload — the event says "this happened involving these entities" and the entities are looked up.

Note: `node_type: "event"` above is a placeholder pending the vocabulary decision in [§10](#10-open-decisions) — high-frequency perception events, alarm-styled error/divergence detections, and manually-injected human events all currently want this one name, and they need distinct types and distinct visual weight. An "entity appeared" must never render like an alarm.

### 7.4 Snapshot slots (fixed cardinality)

A few singleton nodes whose ID never changes, updated in place — a fast way to answer "what is true now" without walking the event log:

```
snapshot:current_phase
snapshot:current_activity
snapshot:active_entity_set        # holds the current list of active entity_ids
snapshot:current_vitals_summary
```

Updated on change, in place. Count doesn't grow with time.

### 7.5 When to emit events (the change-diff logic)

Perception runs every window, but emits an event only when the diff crosses a change threshold. Everything else is a silent in-place snapshot update. Per window:

- **Visible entity set** vs. previous window's active set: new IDs → `entity_appeared`; IDs no longer present → `entity_disappeared`; same set → no event, just update `last_seen_window`/`is_active` in place.
- **Instrument-anatomy relations** (e.g., needle_driver → grasping → needle) vs. previous window: new → `relation_started`; ended → `relation_ended`; ongoing → no event.
- **Activity description** vs. previous window's, semantically: only `activity_changed` if materially different (debounced, §7.6).
- **Phase** (from the structural signal): only `phase_changed` if the ID differs from the current snapshot's phase.
- Nothing changed → emit nothing to the event stream; just update entity counters and the perception agent node's `last_active_window`.

Downstream reasoning steps subscribe to the event stream (via SSE), not to per-window perception. If no events fire in a window, nothing downstream triggers. **Silence is the correct output when nothing happened.**

### 7.6 Debouncing (stop flicker and near-duplicates)

- **Entity flicker**: don't emit `entity_disappeared` on the first missing window — require 2–3 consecutive absent windows. Require 2 consecutive present windows to flip `is_active` back on (entity node persists continuously regardless).
- **Activity description flicker**: normalize (lowercase, strip punctuation, canonicalize verbs) before comparing. Emit `activity_changed` only if the normalized string differs *and* the change persists for 2+ windows.
- **Relation flicker**: 2-of-3 rule to start, 3-consecutive-absent to end.
- **Confidence smoothing**: exponential moving average on entity `attrs`, never per-window replacement.

None of this changes the model's underlying output — it's a deterministic post-processing layer between the perception call and the event-emission decision. The model still runs every window; the graph just doesn't hear about non-events.

### 7.7 Rate ceilings (defensive backstop)

Even after change-diff and debouncing, hard ceilings guard against a genuinely chaotic segment:

- At most 1 `activity_changed` event per 15 seconds — a second genuine change within that window waits or coalesces.
- At most 5 `entity_appeared`/`entity_disappeared` events per window — excess batches into a single `state_summary` event.
- Coalescing: >3 relation events involving the same entity within 5s → one `relation_burst` event with all sub-changes in the payload.

Enforced deterministically after the reasoning call, before the state-mutation write. Purpose: keep the event stream legible for the UI and downstream reasoners; raw model outputs are still logged (§7.9), just not promoted to events.

### 7.8 Periodic heartbeat (not per-window)

Every 60 seconds, if no events have fired, emit one lightweight `state_summary` event with the current snapshot in the payload (active entity set, current activity, current phase) — proves perception is alive during long steady-state stretches without a per-window "still going" node. Heartbeats render visually distinct in the UI (small marker, not a full node card) or are filtered out of the default view.

### 7.9 Full-fidelity audit trail (off the graph)

Two separate stores:

- **Graph** = curated event stream + snapshot + entities. Bounded, legible, streamed to UI, consumed by downstream reasoning.
- **Audit log** = per-window structured JSON of every raw perception output, appended to a Firestore subcollection (`cases/{case_id}/perception_raw/{window_index}`), never rendered on the graph, never read in the live decision path. Useful for post-hoc analysis, retraining data, benchmark alignment.

This separation is the key architectural discipline: the graph is the reasoning surface, the audit log is the record. Conflating them is what causes the flood.

### 7.10 Emission cadence, summarized

Per 5s window:

1. Reasoning call runs (~1–2s) → raw output.
2. Raw output writes to audit log (Firestore subcollection, fire-and-forget).
3. Change-diff runs deterministically against previous window's raw output.
4. Debounce logic runs (2-of-N rules, string normalization, EMA smoothing).
5. Entity registry nodes updated in place — silent, no events for updates alone.
6. Snapshot slots updated in place — silent.
7. Rate ceilings applied to any pending event emissions.
8. Zero, one, or a small number of event nodes written to the graph via `apply_state_patch`.
9. If no events for 60s of steady state, one heartbeat `state_summary` event.

---

## 8. Frontend panel restructuring

Two changes to the current 6-panel layout:

1. **Replace** the "Active Perception & Retrieval" panel (`ui/frontend/src/components/tiles/RetrievalPanel.tsx`) with a **"Synthetic Patient Profile"** panel — static per-case fields from §2 (ASA class, BMI, prostate volume, nerve-sparing plan, comorbidity flags), loaded once when the case opens, clearly labeled synthetic.
2. **Merge** the perception/retrieval activity feed that panel used to show into the **"Autonomous Action Log"** panel (`ActionLogPanel.tsx`), which becomes the single unified feed for: alerts, artifacts, and activities (web search / literature retrieval calls, escalations, HITL notifications, imaging/retrieval agent activity), instead of only routing/verifier decisions as today.

The panel *slot* gets replaced by the patient profile; the perception/retrieval *content* it used to show folds into the Action Log rather than being dropped.

Net panel set:

| Panel file | Was | Becomes |
|---|---|---|
| `VideoPanel.tsx` | Raw video | unchanged |
| `AnnotatedVideoPanel.tsx` | exists, not mounted (removed for playback-sync issues) | stays unmounted unless separately revisited |
| `StateGraphPanel.tsx` | Living State Graph | gains the new node/edge kinds from §4 and the timestamp-ordering affordance from §4.3 |
| `EventInputPanel.tsx` | "Manual Event Input / Monitor Feed" | unchanged |
| `RetrievalPanel.tsx` | "Active Perception & Retrieval" | **Synthetic Patient Profile** |
| `ActionLogPanel.tsx` | "Autonomous Action Log" (routing/verifier only) | alerts + artifacts + activities: literature retrieval, escalations, HITL notifications, plus what `RetrievalPanel` used to show |

A benchmark/documentation surface (the scorecard + operative-note draft from workflow steps 11–12) has no home in these six — likely a modal or dedicated post-case view triggered from the `Benchmark`/`Documentation` graph nodes rather than a permanent seventh tile. See [§10](#10-open-decisions).

---


## 9. Existing assets — reuse, adapt, retire

The current codebase is a parts bin for this plan, not a baseline it has to respect. Inventoried against the real repo as of 2026-08-15 (`state/schema.py`, `agents/`, `services/`, `tools/`, `ui/frontend/src/graph/`), sorted by how each piece lands here. `docs/agentic_workflow.md` §15 carries the same inventory from the orchestration side.

**Reuse as-is:**

- Single-write-path discipline (`apply_state_patch`, already `async def`), per-case Firestore isolation, fresh-`case_id`-per-open, and the SSE diff stream to the frontend.
- Static-hierarchy-before-dispatch and concurrent sweep dispatch via `asyncio.gather`.
- `Provenanced` on every node and edge.
- Video/frame utilities: duration, fps, window generation, frame sampling, multimodal content assembly.
- `tools/fhir_write.py` (readback-verified) and `tools/europepmc_rag.py`.
- The ground-truth wall: `tools/sedmamba_labels.py` confined to the validation sidecar, guarded by an import-inspection test.

**Adapt** — core is right, shape changes:

| Asset | Change |
|---|---|
| Scene Graph Builder → **Perception Sweep** | The reasoning call largely survives; the entire change-diff / debounce / rate-ceiling / snapshot-slot / audit-log layer in §7 is new around it. Heaviest rework in the plan. |
| Monitor → **Error Detection Sweep** | Cascade mechanics unchanged (CARES-style 3-role, deterministic weighted aggregation, macro-F1 0.515 measured — see `docs/validation_results.md`). Emits onto the new error node kind and becomes a real event source for downstream agents. |
| Anticipation → **Anticipation Sweep** | Mechanics survive; cadence and its relationship to the shared window grid to be settled. |
| `state/schema.py` vocabulary | `NodeEntityType` (`agent\|phase\|entity\|artifact\|event`) and `EdgeKind` (`predicted\|action\|observed\|revised`) expand to §4's kinds — a migration, not additive-only. |
| `POST /events/manual` | Needs structured `event_kind` + `target_node_id` + `outcome` to carry both HITL surfaces; today it accepts only `{case_id, text}`. |
| `palette.ts` + `Legend.tsx` | Color currently keys off **agent identity** plus edge kind, with one special-cased node accent. §4 keys off **node kind** — a different axis that has to coexist with agent identity, not replace it. Legend rebuilt to match; it must stay factually accurate against the real rendering code. |
| Graph layout (`layout.ts`, `nodeTypes.tsx`) | Fixed-width truncated labels → variable-length nodes with measured-width-driven auto-reflow (§4.1). |
| `RetrievalPanel` / `ActionLogPanel` | Restructured per §8. |
| Offline validation scripts | Scoring logic lifts into the Benchmark Agent as a per-case graph-writing step; the CLI wrappers stay for offline corpus-wide use. |

**Build** — nothing exists: the in-process graph-change bus; Complication Reasoning, Literature Retrieval, Corrective Replanning, Trajectory Divergence Detection, Alert Routing, Verification Gate, Benchmark, Documentation; both external-write executors; the patient twin and vitals stream; the bounded corrective-action library; the context-slice assembler; the perception audit-log subcollection; both HITL surfaces; the orphaned-edge reconciliation check; OpenTelemetry instrumentation.

**Retire:**

- The Slack-webhook alert destination — replaced by a healthcare-relevant destination, TBD (§6 step 10).
- `TrajectoryPatch`'s `candidate_plan` / `recovery_option` kinds, if Corrective Replanning's proposal shape supersedes them. Nothing populates them today.
- Fixed-width label truncation in the graph.

**Sequencing note:** Monitor and Anticipation read the same graph that Perception is being rebuilt underneath. Sequence the build so they aren't destabilized mid-rework — settle the node vocabulary migration first, since every other change depends on it.

---

## 10. Open decisions

1. **Alert destination platform** — open, explicitly not Slack. Criteria: free/public sandbox with no approval wait, a real API rather than just a webhook, clinical plausibility as an alert channel.
2. **Node-type vocabulary for `event`.** Three distinct things want this name: high-frequency perception events (§7.3), alarm-styled error/divergence detections, and manually-injected human events. They need distinct types and distinct visual weight.
3. **Divergence naming.** "An error was detected" and "the actual trajectory departed from the proposed corrective plan" are different signals — §4.1 already separates them as node kinds (`Error` vs. `Divergence-alert`); the schema needs to as well, rather than one overloaded model.
4. **Color axis coexistence** — how node-kind color and agent-identity color combine on one node (fill vs. border, or icon vs. fill).
5. **Where the benchmark scorecard and documentation draft render** — modal, dedicated post-case view, or a seventh panel.
6. **Error severity.** Complication reasoning triggers on "error node above severity threshold"; error nodes currently carry `confidence` and `composite_score`. Severity needs defining and storing, or the trigger expressing in terms of what exists.

`docs/agentic_workflow.md` §16 carries the orchestration-side decisions (perception `monotonic_seq` ownership, divergence-detection idempotency, `awaiting_approval` durability, Anticipation cadence).
