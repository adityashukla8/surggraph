# SurgGraph

An autonomous multi-agent system that watches a robot-assisted radical prostatectomy (RARP) video in real time, builds a live "Living Graph" of everything it perceives and reasons, detects technique errors, reasons about downstream clinical complications grounded in retrieved literature, proposes corrective actions from a bounded action library, detects when the surgeon's actual behavior diverges from a proposed plan, and — after passing a fail-closed verification gate — writes real external records (a FHIR `Communication` alert and a FHIR `DocumentReference` operative note) to a public HAPI FHIR server. Two human-in-the-loop surfaces let a surgeon acknowledge/dismiss proposals and approve/edit/reject the final documentation before anything is filed.

This document is generated directly from the code as it exists in this repository — no external design docs were used as a source. Every claim below is backed by a specific file.

---

## 1. What actually happens, end to end

A user presses play on the demo video in the browser. That triggers `POST /cases/open` on the orchestrator service, which:

1. Mints a brand-new `case_id` (`case-<12 hex chars>`) — every case is fully isolated, no shared state between concurrent viewers.
2. Fires a tiny "prime" Gemini call and writes the case's static skeleton (trigger node, synthetic patient twin, and every agent node in the topology) concurrently.
3. Registers the four event-driven agents on an in-process event bus.
4. Runs **two concurrent sweeps** over the video: **Perception** (what is happening) and **Error Detection** (what is going wrong), one 5-second window at a time.

From there the rest of the pipeline is **event-driven**, not scheduled — each stage wakes only when a specific kind of node lands on the graph:

```
 Perception sweep                    Error Detection sweep
 (entities, relations,               (3 role sub-agents, weighted
  activity, phase)                    aggregation, escalation)
        |                                     |
        |                              error node (severity >= medium)
        |                                     |
        |                          Complication Reasoning
        |                          (formulates literature queries,
        |                           retrieves papers, reasons about
        |                           complications with evidence backing)
        |                                     |
        |                           complication node
        |                                     |
        |                          Corrective Replanning
        |                          (selects from a bounded action
        |                           library, or escalates)
        |                                     |
        |                        corrective_trajectory node
        |                                     |
        +------------------------> Divergence Detection
                                    (polls: did the same error recur?
                                     is what perception observes
                                     consistent with the plan?)
                                             |
                                    divergence_alert node
                                             |
                                     Alert Routing
                                    (assembles payload, calls the
                                     Verification Gate)
                                             |
                              +--------------+--------------+
                              |                             |
                        gate PASSES                    gate BLOCKS
                              |                             |
                   FHIR Communication write        verification_block node
                   (real HTTP POST + readback        (no external write)
                    to hapi.fhir.org)

 [ meanwhile: a surgeon can acknowledge/dismiss any corrective_trajectory
   via HITL #1 — acknowledging silences future alerts but keeps monitoring ]

 After both sweeps finish and in-flight event handlers drain:
        Benchmark Agent  ->  grades every error node against real ground truth
        Documentation Agent  ->  drafts an operative note from the whole graph
                                   (approval_status: pending)

 [ a surgeon approves/edits/rejects via HITL #2 — only on approval does the
   note pass the Verification Gate and get filed as a FHIR DocumentReference ]
```

Every one of these steps is a real write to a shared graph (the "Living Graph"), streamed live to the browser over Server-Sent Events. Nothing is a mock — the literature retrieval hits real Europe PMC, the FHIR writes hit a real public FHIR server and are read back to confirm they landed, and the ground truth used for grading is a real SEDMamba annotation file, never seen by any detection agent before scoring.

---

## 2. Architecture

Three long-running processes:

| Process | What it is | Entry point |
|---|---|---|
| **State service** | FastAPI. The single writer of the Living Graph. Firestore-backed. | `services/state_service/main.py` |
| **Orchestrator service** | FastAPI. Receives `POST /cases/open`, runs the whole agent pipeline as a background task. Also serves the two HITL endpoints. | `services/orchestrator_service/main.py` |
| **Frontend** | React + Vite + TypeScript. Renders the video, the live graph (ReactFlow), and four side panels. | `ui/frontend/src/App.tsx` |

Every agent module is plain Python (`agents/`) built on Google's Agent Development Kit (`google-adk`). Reasoning agents are real `LlmAgent` instances calling Gemini through Vertex AI; a handful of steps (aggregation math, severity scoring, the verification gate, benchmark scoring) are deliberately **not** LLM calls — see §5 for exactly which and why.

### 2.1 The Living Graph — the system's actual state

There is no `CaseState` object passed between agents. State lives entirely in a shared, typed graph (`state/schema.py`), and every agent reads/writes it through `tools/state_tools.py`. Two write primitives:

```python
apply_state_patch(case_id, node=..., edge=..., reason=...)          # one node or edge
apply_state_patches(case_id, [(node, edge, reason), ...])           # a batch, one transaction
```

Both POST to the state service (or, with no `STATE_SERVICE_URL` set, fall back to a local `data/runtime/{case_id}_graph_patches.jsonl` file — used by scripts/tests). The batch form was introduced specifically because Firestore's transactional write overhead (~87% of a single write's latency is the transaction itself, not the network) makes N separate writes far more expensive than one batch of N — see `agents/error_detection/agent.py` and `agents/perception/agent.py`, where a whole window's writes are collected and sent as one call.

**19 node types** (`state/schema.py::NodeType`):

| Group | Types |
|---|---|
| Structural | `trigger`, `agent`, `patient_twin` |
| Perception | `entity`, `perception_event`, `snapshot`, `phase`, `vitals`, `manual_event` |
| Reasoning chain | `error`, `complication`, `literature_evidence`, `corrective_trajectory`, `divergence_alert` |
| Action & safety | `action_intent`, `verification_block`, `action_outcome` |
| Post-case | `benchmark`, `documentation` |

**13 edge kinds** (`EdgeKind`): `detection`, `causal_reasoning`, `evidence`, `prediction`, `proposal`, `trajectory_comparison`, `confirmation`, `verification`, `grading`, `hierarchy`, `involved`, `outcome`, `succession`.

Node IDs are never hand-formatted by an agent — every ID comes from one module, `state/node_ids.py`, e.g. `error(window, category) -> "error:{window}:{category}"`, `complication(root_error_id, name) -> "complication:{root_error_id}:{slug}"`. This is what keeps edges from silently dangling when two agents disagree on a convention. Edge IDs are deterministic from `(source, target, kind)`, so re-writing the same logical edge updates it in place rather than duplicating it.

**Why the graph, not a shared object:** every reasoning call gets a *slice* of the graph assembled fresh by plain Python (`tools/context_slice.py`) before dispatch — never a raw tool call the model has to make itself. This keeps context assembly deterministic (same graph in, same slice out) and keeps agents from having to reason about how to query state.

### 2.2 How a write becomes visible: Firestore, the event bus, and SSE

`services/state_service/store.py` (`CaseGraphStore`) is a genuinely multi-tenant, multi-instance-safe backing store:

- One Firestore document per case (`cases/{case_id}`) holds a monotonic `seq` counter.
- One subcollection per case (`cases/{case_id}/graph_items/{item_id}`) holds every node and edge, discriminated by a `kind` field. `item_id` is `node:{node_id}` or `edge:{edge_id}`.
- Every write (single or batched) is a real Firestore transaction that reads the case's current `seq`, increments it, and writes the item(s) with their assigned `seq` — so concurrent writers to the same case serialize correctly instead of racing.
- `remove_edge` is never a physical delete (a deleted doc has no data left for a listener to reconstruct an event from) — it's a `.set()` overwrite carrying `op="remove_edge"`, filtered out by `snapshot()`.
- Real-time fan-out uses Firestore's native `on_snapshot` listener, scoped to `where(seq > baseline_seq)` with the baseline captured *before* attaching — closing a real, previously-observed data-loss window where a client's `GET /snapshot` and its SSE `GET /stream` attach were two separate moments, and anything written in between was lost. The frontend passes the snapshot's own `seq` as `?since_seq=` when it opens the stream (`ui/frontend/src/graph/useCaseStateStream.ts`).

Inside the orchestrator process, `state/event_bus.py` is a **separate**, in-process pub/sub layer — not Firestore listeners. Event-driven agents (Complication Reasoning, Corrective Replanning, Divergence Detection, Alert Routing) subscribe to it, filtered by node type:

```python
bus.subscribe("complication_reasoning", handler, node_types={"error"})
```

`apply_state_patch`/`apply_state_patches` call `event_bus.publish(case_id, event)` immediately after each write commits — so a handler triggered by a write always sees a graph that already contains what triggered it. This only works because one case's agents all run inside the one background task that opened it (`agents/orchestrator/agent.py::open_case`); the module docstring is explicit that this invariant would break if a case's agents were ever split across processes.

At case close, `event_bus.close_bus` drains every in-flight handler (bounded by a timeout derived from Divergence Detection's own poll budget) before the post-case agents run, so a complication-reasoning pass triggered by the very last error doesn't get cancelled mid-flight.

---

## 3. The agent roster

### 3.1 Orchestrator (`agents/orchestrator/agent.py`)

Not an LLM-driven decision maker — a plain async function, `open_case(case_id, video_id, start_s, end_s)`, wrapped in a thin `OrchestratorAgent(BaseAgent)` for ADK Registry visibility. Its real job:

1. Resolve the sweep range: explicit args win, else `SURGGRAPH_SWEEP_START_S`/`SURGGRAPH_SWEEP_END_S` env vars (development bound), else the video's real full duration (read via `cv2`, never assumed).
2. Concurrently: (a) draw the entire static skeleton — trigger node, patient twin, every top-level agent node, Error Detection's aggregation node and 3 sub-agent nodes, all hierarchy edges — as one batched write, and (b) fire the prime Gemini call.
3. Subscribe the four event-driven agents to the case's event bus.
4. Concurrently run `error_detection_case(...)` and `perception_case(...)` to completion.
5. Drain the event bus (budget = Divergence Detection's `MAX_POLLS * POLL_INTERVAL_S + 30`).
6. Run `benchmark_case(...)` then `draft_note(...)`, each independently try/excepted so one failing never blocks the other or fails case close.

The **prime call** (`_prime_gemini`) is a throwaway `LlmAgent` built once at import, using the exact same model/location config every real agent uses, fired through the same `run_llm_agent_once` path — the intent is to pay any one-time cold-start cost on Vertex AI's serving path before the two big sweeps make their first real calls. It is best-effort: a failure is logged and the sweeps proceed regardless.

**Dispatched agents** (`_TOP_LEVEL_AGENTS`): `error_detection_coordinator`, `perception`, `complication_reasoning`, `literature_retrieval`, `corrective_replanning`, `divergence_detection`, `alert_routing`, `verification_gate` — drawn in the static skeleton up front (event-driven ones get a node before they've ever fired, so their eventual output has somewhere to attach).

### 3.2 Perception Sweep Agent (`agents/perception/`)

**Trigger:** dispatched at case open, runs continuously over `[start_s, end_s)` in strictly sequential, non-overlapping 5-second windows (`DEFAULT_WINDOW_S`, env `SURGGRAPH_WINDOW_S`). Sequential is deliberate — window N's entity-registry diff depends on window N-1's accumulated state.

**Model call, one per window:** `agents/perception/subagent.py` builds a single `LlmAgent` (`new_agent_model()` — no role split, unlike Error Detection). Real system instruction (verbatim, `_INSTRUCTION`):

> "You are the Perception Agent, observing one short window of a robot-assisted radical prostatectomy (RARP) surgical video. Report what you can actually see in THIS window: every surgical instrument, anatomical structure, and material... the real relations between them... the current surgical activity, in your own words. STABLE IDS MATTER MORE THAN ANYTHING ELSE HERE... If you see the same real-world object again, REUSE ITS EXISTING ID EXACTLY..."

**Input, per window:** 5 locally-extracted JPEG stills (native resolution, `tools/video_utils.py::sample_frames`), plus a text preamble built by `agents/perception/agent.py::_perceive_window` containing: the window's time range, the list of entity IDs already active in this case (so the model can reuse them verbatim — pulled from `tools/context_slice.py::perception()`), the previous window's activity label, and an *opaque* numeric phase/action ID from the real SAR-RARP50 annotation file (`tools/action_labels.py`) as a hint only — never a name, never told back to the model.

**Structured output** (`PerceptionWindowOutput`): `entities: list[PerceivedEntity]` (stable_id, kind, label, confidence), `relations: list[PerceivedRelation]` (subject_id, verb, target_id, confidence), `activity_description: str`, `reasoning: str`.

**What happens to the raw output — this is the load-bearing part:** the raw model output is *never* written to the graph directly. It passes through a **deterministic, pure Python** change-diff layer (`agents/perception/pipeline.py::PerceptionPipeline`) that decides what actually gets promoted to a graph event:

- **Entity debounce:** an entity must be absent 3 consecutive windows before it's marked "disappeared" (tolerates single-window occlusion); 2 consecutive windows present before a disappeared entity is marked "reappeared". Confidence is tracked as an EMA (`α=0.3`).
- **Relation debounce:** a relation starts once seen in 2 of the last 3 windows; ends after 3 consecutive absent windows.
- **Activity change debounce:** activity descriptions are normalized (lowercased, verb-canonicalized, stopwords stripped, tokens sorted — so "dissecting the bladder neck" and "bladder neck dissection" collapse to the same string) and a *departure* from the current activity must persist 2 consecutive windows before it's adopted — plus a 15-second minimum interval between activity-change events.
- **Rate ceilings:** at most 5 entity events per window (excess batched into one `state_summary`); more than 3 relation events on one entity within 5 seconds coalesce into one `relation_burst`.
- **Heartbeat:** if 60 seconds pass with zero emitted events, one lightweight "steady" event proves liveness.

Steady state emits **nothing** — this is deliberate, so downstream event-driven agents never fire on noise and the graph stays readable over a full case. The full raw output of every window (including suppressed windows) is separately written to a Firestore audit subcollection (`services/state_service/store.py::write_perception_audit`) — never rendered, never read by any live decision, purely for post-hoc analysis.

**What reaches the graph, batched into one write per window** (`agents/perception/agent.py::_emit`): updated `entity` nodes, new `perception_event` nodes for whatever the diff promoted, the `snapshot:current_activity`/`snapshot:current_phase`/`snapshot:active_entity_set` slots updated in place, a new `phase` node on a real phase change (labeled with the model's own semantic activity text, never a bare ID), a `succession` edge chaining each new phase to the previous one (the graph's chronological spine), and every event linked via a `hierarchy` edge to the phase it happened during.

**Synthetic vitals** (`tools/vitals_stream.py`) ride the same per-window batch. A deterministic function of `(video_time_s, case_duration_s)` — same second always yields the same sample (a fixed-seed hash of the timestamp provides noise, so nothing is truly random). Modeled on real RARP physiology: pneumoperitoneum + steep Trendelenburg raises peak airway pressure and EtCO₂, raises MAP modestly, and two scripted excursions (a hypotensive episode ~42-52% through the case, a CO₂-retention episode ~68-78% through) exist specifically to exercise the complication-reasoning path. A `vitals` node is written only when a channel deviates from the *expected* trajectory for that point in the case (not the raw baseline — insufflation itself isn't a deviation) by more than a fixed per-channel threshold.

### 3.3 Error Detection (`agents/error_detection/`)

**Trigger:** dispatched at case open, runs concurrently with Perception over the same range, in non-overlapping 5-second windows.

This is the most structurally complex agent — modeled on CARES (a published zero-shot multi-agent surgical-error-detection architecture), with three independent role perspectives and a deterministic weighted-consensus step.

**Per window, real call sequence:**

1. **Screen pass — 3 concurrent calls, one per role** (`temporal`, `spatial`, `procedural`; `agents/error_detection/subagents.py::build_subagent(mode="screen")`). Each role gets its own frame sample (`STILL_FRAME_PROFILE`: temporal 5 frames @ 960×540, spatial 4 frames native res, procedural 3 frames native res) and a shared instruction pattern:

   > "You are the TEMPORAL analysis agent. Focus on timing, motion, and sequence... For EACH of the six error categories below, decide whether you suspect that category of error is present in this window, based only on what you can actually observe..." followed by the full 6-category OCHRA knowledge block (see below).

   Output (`ScreenOutput`): a list of `CategoryOpinion` (category, suspected, confidence, observation) — one per category the role formed a real opinion on; a category can be omitted entirely rather than guessed.

2. **Escalation — deterministic, no model call** (`agents/error_detection/aggregation.py::pick_escalation_candidate`): across all 3 roles' opinions, picks the single category with the highest confidence, if any cleared `ESCALATION_CONFIDENCE_BAR = 0.4`. Otherwise no escalation happens this window.

3. **Deep pass — 3 concurrent calls, only if something escalated** (`mode="deep"`), same 3 roles, same frames, but framed as a focused re-examination of the *one* escalated category at a fixed `"attending"` expertise tier:

   > "A screening pass flagged this window as a candidate for the '{category}' error category. Now give it a focused, deeper look... Apply balanced clinical judgment: weigh the indicators below against the surgical context you can infer from the frames..."

   Output (`DeepOutput`): `error_present: bool`, `confidence: float`, `reasoning: str`.

4. **Aggregation per category — deterministic, no model call** (`agents/error_detection/aggregation.py::aggregate`): `composite = 1.2·O_temporal + 1.0·O_spatial + 0.8·O_procedural`, fires if `composite > 1.7`. Weights and threshold are project-authored (CARES doesn't publish tuned values), chosen so no single role's dissent can fire an error alone but any two agreeing roles can. For the escalated category, `O_*` comes from the real deep-tier verdict; for every *other* category, `O_*` falls back to that role's own screen-pass `suspected` boolean — an explicit, disclosed design (`CategoryResult.reviewed: "deep" | "screen"` distinguishes the two).

**The 6 error categories and their knowledge scaffold** (`agents/error_detection/knowledge.py::ERROR_KNOWLEDGE_LIBRARY`) — CARES' own published taxonomy, each with a hand-authored `definition`, `normal_indicators`, `error_indicators`, `focus_areas`, real SEDMamba fine-type codes it subsumes, and project-authored `tis`/`cis` (Technical Intricacy / Clinical Impact Score, 1–3): `multiple_attempts`, `out_of_view`, `needle_handling`, `tissue_handling`, `suture_handling`, `instrument_control`.

**What reaches the graph, one batched write per window** (`agents/error_detection/agent.py`): the 3 sub-agent role nodes' cumulative time ranges widened, one `detection` edge per role into the aggregation node carrying that role's real reasoning text (whether or not the window fired), and — for every category that actually fired — an `error` node (labeled with severity, category, confidence, composite score, video time, reasoning) plus a `detection` edge from the aggregation node.

**Severity** (`agents/error_detection/severity.py`) is computed separately from confidence — `0.65·clinical_impact(category) + 0.35·detection_strength(composite, threshold)`, banded into low/medium/high. This is what Complication Reasoning actually triggers on (`meets_complication_trigger`, threshold = `"medium"`), not raw confidence — a detector can be very confident about a trivial error and unsure about a dangerous one.

### 3.4 Complication Reasoning (`agents/complication_reasoning/`)

**Trigger:** event-driven, subscribes to `error` nodes at or above severity `"medium"`. Deduplicated per `(case_id, error_category, current_phase, patient_profile_id)` — the same category reasoned about twice in one phase is a repeat question.

**Two real Gemini calls per error**, both text-only:

1. **Query formulation** (`build_query_agent`) — reads the error, patient twin, vitals trend, current activity, and recent related errors, and composes 3–5 short independent search queries. The instruction is unusually detailed because Europe PMC is a **boolean AND** engine, not semantic search — every extra word deletes candidates rather than refining them (measured: a 4-word query returned 608 hits, the same idea at 10 words returned 1). Output (`LiteratureQuery`): `queries: list[str]` (2–5), `rationale`, `initial_concerns`.

2. **Reasoning** (`build_reasoning_agent`) — reads the same context plus the retrieved abstracts (numbered) and names 0–3 complications worth surfacing. Each candidate (`ComplicationCandidate`) carries `name`, `mechanism` (one sentence), `patient_specific_factor`, `confidence`, `supporting_citation_index` (or `None`), and `evidence_backed: bool` — **true only if a real retrieved abstract supports it**; the model is explicitly instructed never to stretch a loosely-related paper into a citation.

**Retrieval, between the two calls** (`agents/literature_retrieval/agent.py::retrieve`): every query fans out concurrently to a real Europe PMC REST call (`tools/europepmc_rag.py`, `live=True`), per-case per-query cached by SHA-256 hash. Results merge via **Reciprocal Rank Fusion** (Cormack, Clarke & Buettcher, SIGIR 2009, `k=60`) — no relevance score exists on a live Europe PMC search, so RRF's rank-position-only merge is the correct tool; a paper surfacing under more than one independently-phrased query outranks one that only topped a single list. Each retrieved paper becomes a `literature_evidence` node, hung off whatever prompted the search (the error node) via a `hierarchy` edge — "consulted," not "supports."

**What reaches the graph:** one `complication` node per candidate, a `causal_reasoning` edge from the root error, and — only for the specific paper the model actually cited — an `evidence` edge from that `literature_evidence` node. The triggering error node is updated with a `complication_status` (`"reasoned"`, `"none_warranted"`, `"already_reasoned"`, or `"below_severity_threshold"`) so the graph never shows an unexplained absence.

### 3.5 Corrective Replanning (`agents/corrective_replanning/`)

**Trigger:** event-driven off `complication` nodes.

**One real Gemini call.** The critical design constraint: **the model selects, it never generates.** `CorrectiveProposal`'s schema has no field for free-text clinical instructions — only `action_id: str` (validated against a library afterward), `order: int`, `why_this_action: str`. The instruction:

> "YOU MAY ONLY SELECT FROM THE LIBRARY. Return the action_id of each action you are choosing... You cannot write an action of your own — there is nowhere in the output to put one... If nothing in the library genuinely fits this situation, set escalate=true... A forced weak match is worse than an honest escalation."

**Enforcement, not trust:** even though the prompt asks the model to stay inside the vocabulary, `agents/corrective_replanning/library.py::resolve` drops any `action_id` that isn't genuinely in that category's library before it can reach the graph — a hallucinated ID cannot get through regardless of what the model outputs.

**The library itself** (`data/corrective_actions/library.json`) is real, reviewable JSON data, not code — one of the few places clinical *content* (not reasoning) is legitimately hand-authored, with its own `_provenance` block disclosing it as "tier 2: derived from published OCHRA error descriptions, not a clinical guideline, not reviewed by a practising surgeon." 6 categories, 2–3 actions each, each action carrying `action`, `rationale`, `verification_check` (what Divergence Detection later measures against), `inverts_indicator`.

**What reaches the graph:** either a `corrective_trajectory` node (dotted-outline in the UI, marking it as a proposal rather than a fact) with its resolved steps and the library's provenance carried onto the node, or — on escalation / zero resolved actions — an escalation node with no steps. Both get `proposal` edges from the complication and the root error.

### 3.6 Trajectory Divergence Detection (`agents/divergence_detection/`)

**Trigger:** event-driven off `corrective_trajectory` nodes (skips escalations — nothing to diverge from). Once active, polls the proposal every `POLL_INTERVAL_S = 5.0` seconds, up to `MAX_POLLS = 8` times (40s max per proposal), stopping early once resolved.

**Deterministic-first, LLM second** — two genuinely different questions:

1. **Deterministic pass, every poll, free:** did the same error category the proposal addresses fire *again* after the proposal was made? If so, that's the strongest available evidence a corrective didn't take effect — written as an alert immediately, `detection_method: "deterministic"`, confidence 0.9, no model call.

2. **Semantic pass, only if there's enough new evidence** (`MIN_OBSERVATIONS_TO_JUDGE = 2` new `perception_event`s since the proposal): one real Gemini call (`agents/divergence_detection/subagent.py`) comparing the proposal's steps and their verification checks against what perception has actually observed since. Output (`DivergenceJudgment`): `aligned: bool | None` (a real third state — "cannot tell" is a legitimate, non-guessing answer), `confidence`, `satisfied_steps`/`unsatisfied_steps` by order, `reasoning`.

> "aligned=null if the observations genuinely do not let you tell... A false divergence alert interrupts a surgeon mid-operation, so silence is much better than a guess."

**Deduplication, two layers:** one alert per proposal, and — because one error can spawn several complications, each with its own proposal, each independently noticing the same recurrence — one alert per underlying *evidence*, across proposals, so a surgeon hears "you were asked to fix X and it happened again" once, not five times.

**Advisory mode (HITL #1 interaction):** if the proposal was acknowledged by a surgeon, divergence detection keeps polling but marks any alert `advisory: True`, which Alert Routing refuses to route externally — acknowledgment silences the alert path, it does not stop monitoring.

**What reaches the graph:** a `divergence_alert` node, a `trajectory_comparison` edge from the proposal, and a `detection` edge from every real observation node that evidenced it.

### 3.7 Alert Routing (`agents/alert_routing/`)

**Trigger:** event-driven off `divergence_alert` nodes. **No model call** — every field in the alert payload is already on the graph, written by the agent that reasoned it; re-generating it here would let a second model paraphrase a clinical claim its author never made.

Mechanically assembles the full reasoning trail (root error, complication with its literature citations, proposal steps, divergence reasoning, provenance tier) into a payload, writes an `action_intent` node **before** anything leaves the system (`status: "awaiting_verification"`), then calls the Verification Gate *synchronously*. Only on a pass does it call `tools/fhir_alert.py::send_alert`. Either way, a real `action_outcome` node records what actually happened — delivered, blocked, or failed — so an intent is never left ambiguous.

### 3.8 Verification Gate (`agents/verification_gate/gate.py`)

**No model call, ever** — every check is a structural fact about the graph (does this node exist, is this attribute true, is this number above this floor). Deliberately **fail-closed**: a missing node, absent attribute, or exception mid-walk blocks, never passes. Structurally **read-only**: this module imports no write tool, no alerting tool, no FHIR tool — it returns a verdict; the caller performs the write.

**`evaluate_divergence_alert`** walks divergence → proposal → complication → root error, checking at each step: the node resolves, the proposal isn't an escalation, it has real provenance and steps, **the complication is `evidence_backed`** (the check this gate fundamentally exists for — an ungrounded complication can never become an external alert, regardless of confidence), complication confidence ≥ 0.5, the causal/proposal edges genuinely exist on the graph (not just asserted in attrs), divergence confidence ≥ 0.6, and the alert isn't advisory.

**`evaluate_documentation`** is a different check set for a different artifact: the draft exists and has a summary, ≥4 sections are populated, **`approval_status == "approved"`** (the entire point of HITL #2 — an unapproved note can never reach the clinical record), a benchmark exists for the case (so a reader can weight what they're reading), and limitations are stated.

Every evaluation, pass or block, is written as a `verification_block` node with the full per-check breakdown — passes are recorded too, so an approved external write always shows it was actually checked.

### 3.9 HITL #1 — Acknowledgment (`agents/hitl/acknowledgment.py`)

`POST /cases/{case_id}/hitl/acknowledgment` on the orchestrator service. Applies `"acknowledged"` or `"dismissed"` to a `corrective_trajectory` node in place, and writes a separate `manual_event` node (`source_agent: "human"`) edged to the proposal — the human action is recorded as a human action, never silently folded into the proposal's own attrs as if the system had decided on its own. Acknowledged proposals stay monitored (in advisory mode); dismissed ones stop being monitored entirely.

### 3.10 Benchmark Agent (`agents/benchmark/agent.py`)

**No model call** — deterministic arithmetic over graph state and real ground truth, so scoring can't vary between runs of the same case. Regenerates the exact window grid the live sweep used, checks which windows the case actually fired an `error` node on (from `error:{window_id}:{category}` IDs), and scores against SEDMamba's real per-window ground truth (`data/annotations/{video}/error_annotation.pkl`).

**Scored on the binary error/no-error axis only** — the real ground truth (`error_GT`) is a binary array with no category labels, so per-category precision/recall is not computable from this data and is explicitly not claimed. Reports macro-F1 against CARES' own published 0.543 on this exact dataset as the honest comparison bar, plus descriptive-only (never scored) per-category fire counts. Writes one `benchmark` node with a `grading` edge from every error node it graded. **This is the one legitimate place ground truth enters the system, and only post-hoc** — no live detection code imports the annotation loader.

### 3.11 Documentation Agent (`agents/documentation/`)

**Trigger:** runs once at case close, after Benchmark (so the draft can report how much to trust itself).

**One real Gemini call** over the *entire* case graph — unlike every other agent's filtered slice, documentation deliberately doesn't filter, because the whole point is that the graph already contains the record. The instruction's central rule, repeated because it's the one thing that must never slip:

> "Every clinical statement must be attributable to what actually produced it. WRITE 'Automated analysis flagged possible out-of-view instrument handling at 0:20.' NEVER 'The instrument was handled out of view at 0:20.'"

Output sections (`OperativeNoteDraft`): `procedure_course` (the observed phase progression — genuinely directly observed, stated plainly), `technique_observations` (flagged errors, framed as unconfirmed automated findings), `risks_considered` (complications, each marked literature-grounded or not), `decision_support` (what was proposed and how the surgeon responded — the one section with no analog in a conventional operative note), `physiological_events`, `system_performance` (the benchmark, in plain language — "if precision was poor, that is the most important sentence in the document"), `summary`, `limitations`. Deliberately **no "Complications" heading** — that word means something occurred; these are hypotheses about what could follow a detection that may itself be a false positive.

Written with `approval_status: "pending"` — nothing is filed until HITL #2 approves it.

### 3.12 HITL #2 — Documentation Approval (`agents/hitl/approval.py`)

`POST /cases/{case_id}/hitl/approval`. No parked coroutine anywhere — the docstring is explicit that a background task held open for an approval that might come hours later can't survive a restart or scale-to-zero, so the draft's own `approval_status: pending` on the graph *is* the durable state, and this endpoint does the entire remaining flow synchronously when the surgeon actually acts: apply the decision → gate → real FHIR `DocumentReference` write (`tools/fhir_write.py`, with readback verification) → outcome node. An edit is treated as an approval of the edited text, with the original preserved alongside so the record shows system output vs. clinician sign-off.

---

## 4. External integrations

**FHIR** (`tools/fhir_write.py`, `tools/fhir_alert.py`) — a real public HAPI FHIR R4 server (`hapi.fhir.org/baseR4` by default, `FHIR_BASE_URL` env override). Every write is followed by a real HTTP `GET` readback that field-compares the response against what was sent, and `verified: bool` is reported honestly rather than assumed from a 200/201. Both writers are idempotent: `write_document_reference` stamps an idempotency key into the resource's `identifier` array and checks for a prior write (process-local cache first, then a real search) before creating a duplicate; `send_alert`'s FHIR `Communication` carries an identifier keyed on `(case_id, alert_node_id)` for the same reason. Alerts use FHIR's `Communication` resource (the real HL7 model for "a record of a communication such as an alert") rather than a generic webhook — a deliberate choice for genuine clinical-system integration over plumbing.

**Europe PMC** (`tools/europepmc_rag.py`) — `search_literature(query, k, live)`. `live=True` (what Complication Reasoning always uses) hits the real Europe PMC REST API directly. A cached-corpus path also exists (FAISS `IndexFlatIP`, `all-MiniLM-L6-v2` sentence-transformer embeddings, `data/rag_cache/corpus.jsonl` — 50 pre-fetched open-access abstracts) for a "no live network" fallback mode, but the live agent path always queries live. This module does zero query formulation of its own — the caller (an LLM) composes the query from live case context; no error category is ever mapped to a search term in code.

---

## 5. Synthetic data — disclosed everywhere it surfaces

**Patient twin** (`tools/patient_twin.py`, `data/synthetic/patient_twin.json`) — one authored profile (`display_name: "Synthetic RARP Patient 001"`), loaded once per case and written into the static skeleton. Carries `_disclosure` and `_provenance` fields, and every graph node/prompt rendering of it leads with "SYNTHETIC." D'Amico risk group is *derived* from PSA/Gleason/stage (D'Amico et al., JAMA 1998 criteria), never stored, so it can't drift from the three values that define it. `elevated_priors` (6 declared fields — e.g. prostate volume, median lobe, prior TURP, membranous urethra length, BMI, ASA class) drive both the risk-profile summary text and the UI's prior-marker dots from one shared source.

**Vitals** (`tools/vitals_stream.py`) — deterministic function of the case clock, described fully in §3.2. Every sample and node carries `synthetic: True`.

**The corrective-action library** (`data/corrective_actions/library.json`) — real reviewable data, explicitly tier-2 sourced (derived from the OCHRA-grounded error-knowledge library, not a clinical guideline, not surgeon-reviewed), never guideline text ingested directly.

---

## 6. Frontend (`ui/frontend/src/`)

React 19 + Vite + TypeScript, `@xyflow/react` (ReactFlow) for the graph canvas, `@dagrejs/dagre` for auto-layout.

**Trigger flow:** the video's *first* real `onPlay` event (not page load) calls `POST {ORCHESTRATOR_URL}/cases/open`; the returned `case_id` opens an SSE connection to the state service (`ui/frontend/src/graph/useCaseStateStream.ts`), which applies every `StateDiffEvent` to local node/edge maps keyed by ID with per-item `seq`-based last-write-wins (Firestore's listener delivers changes in whatever order it batches them, not necessarily ascending `seq` — so a global "must be exactly last+1" policy would cause spurious resyncs; per-key seq comparison is what actually matches Firestore's real delivery semantics).

**Panels** (`components/tiles/`):

| Panel | Shows |
|---|---|
| `VideoPanel` | The raw source video. |
| `StateGraphPanel` | The live graph — every node the outline-color-by-kind / icon-color-by-agent axes from `graph/palette.ts`, click-to-collapse/expand any node with children (`graph/useGraphCollapse.ts` — reachability-based, so a node reachable through more than one path stays visible when only one parent collapses), a legend reading from the same label map the nodes tag themselves with. |
| `EventInputPanel` | Manual free-text event injection (`POST /events/manual`, tagged `source_agent: "human"`, never disguised as an inference) plus a live feed of Error Detection's own per-window reasoning, tagged by real `source_agent` (temporal/spatial/procedural/aggregation) with wall-clock and video time. |
| `CaseContextPanel` | The synthetic patient twin (collapsed-by-default detail groups) and live vitals with sparklines, sourced entirely from the graph — no client-side clinical computation. |
| `AutonomousActionsPanel` | A unified timeline of corrective proposals, divergence alerts, the documentation draft, and the benchmark scorecard — each with its own reasoning-trail drill-down, citation list (from real `evidence` edges, not attrs), and — for proposals and the documentation draft — the actual HITL controls that call the orchestrator's approval/acknowledgment endpoints. |

Graph rendering carries every node's kind as visible text (not color alone, so it survives greyscale/colorblind viewing), auto-sized by real canvas text measurement (`graph/measureLabel.ts`) so dagre reserves exactly the space a node will actually render at.

---

## 7. Repository layout

```
agents/                  one directory per agent; empty scaffolds for
                          never-built agents have been removed
  orchestrator/           root dispatcher, case lifecycle
  perception/             sweep agent + deterministic change-diff pipeline
  error_detection/        coordinator, 3-role sub-agents, OCHRA knowledge,
                          aggregation math, severity scoring
  complication_reasoning/ event-driven, 2-call chain
  literature_retrieval/   Europe PMC retrieval + RRF merge
  corrective_replanning/  event-driven, library-constrained selection
  divergence_detection/   deterministic-first + LLM-fallback polling
  alert_routing/          payload assembly + gate call, no LLM
  verification_gate/      fail-closed structural checks, no LLM
  hitl/                   acknowledgment.py, approval.py
  benchmark/              deterministic scoring vs. ground truth
  documentation/          case-close narrative drafting
  anticipation/           built, NOT dispatched by the orchestrator (see below)
  scene_graph_builder/    built, NOT dispatched by the orchestrator (see below)

state/                  schema.py (the Living Graph vocabulary), node_ids.py,
                        event_bus.py (in-process pub/sub)
services/
  state_service/          Firestore-backed graph store + SSE (main.py, store.py)
  orchestrator_service/   case-open trigger + HITL endpoints
tools/                  everything agents call: state_tools, context_slice,
                        adk_runner (the shared Gemini invocation path),
                        gemini_model (model/location config), video_utils,
                        fhir_write, fhir_alert, europepmc_rag, patient_twin,
                        vitals_stream, action_labels, sedmamba_labels,
                        phase_transition_priors, segmentation_masks
data/
  synthetic/patient_twin.json
  corrective_actions/library.json
  rag_cache/               pre-embedded literature corpus (fallback path)
  annotations/video_01/    real SAR-RARP50 phase + SEDMamba error ground truth
  video/                   the source video file(s)
  validation/              accuracy sweep logs
ui/frontend/            React/Vite app
scripts/                validation sweeps, corpus pre-caching, one-off tooling
tests/                  pytest suite
```

**`agents/anticipation/`** and **`agents/scene_graph_builder/`** are real, complete, built modules — the first predicts the next surgical phase from live frames (deliberately never shown or asked for a numeric phase ID, per its own docstring), the second extracts scene entities/relations independently of Error Detection. Neither is imported or dispatched by `agents/orchestrator/agent.py` — confirmed by direct inspection of the orchestrator's import list and `_TOP_LEVEL_AGENTS`. They exist in the codebase but are not part of the live pipeline described above.

---

## 8. Running it

Three processes, three ports:

```bash
uv run uvicorn services.state_service.main:app --host 127.0.0.1 --port 8080
uv run uvicorn services.orchestrator_service.main:app --host 127.0.0.1 --port 8090
cd ui/frontend && npx vite --host 127.0.0.1 --port 5173
```

Key environment variables (all read via `python-dotenv` / `os.environ`):

| Variable | Used by | Purpose |
|---|---|---|
| `SURGGRAPH_PROJECT_ID` | `tools/gemini_model.py` | GCP project for Vertex AI |
| `GEMINI_MODEL` | `tools/gemini_model.py` | Default `gemini-3.5-flash` |
| `GEMINI_LOCATION` | `tools/gemini_model.py` | Default `global` — the model 404s on every tested regional endpoint |
| `FIRESTORE_DATABASE` | both services | Named Firestore database, default `(default)` |
| `FHIR_BASE_URL` | `tools/fhir_write.py`, `tools/fhir_alert.py` | Default `https://hapi.fhir.org/baseR4` |
| `STATE_SERVICE_URL` | `tools/state_tools.py` | If unset, agents fall back to local JSONL files (scripts/tests) |
| `SURGGRAPH_WINDOW_S` | `tools/video_utils.py` | Sweep window size, default `5.0` |
| `SURGGRAPH_SWEEP_START_S` / `SURGGRAPH_SWEEP_END_S` | `tools/video_utils.py` | Optional dev-only bound on how much of the video a case sweeps |
| `VITE_STATE_SERVICE_URL` / `VITE_ORCHESTRATOR_URL` / `VITE_DEMO_VIDEO_ID` | frontend | Service URLs and which video to load |

## 9. Stack

**Backend:** Python ≥3.12, `google-adk` + `google-genai` (Vertex AI, Gemini), FastAPI + `uvicorn`, `sse-starlette` (SSE), `google-cloud-firestore`, `httpx`/`requests`, `opencv-python-headless` (frame extraction), `faiss-cpu` + `sentence-transformers` (literature corpus fallback), `pydantic` v2 (every schema in the system).

**Frontend:** React 19, Vite, TypeScript, `@xyflow/react` (graph canvas), `@dagrejs/dagre` (layout).
