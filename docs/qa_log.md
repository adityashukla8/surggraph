# QA log — functional testing and defects

Every real defect found by running the system, what surfaced it, and how it was fixed. **Add an entry whenever a real run turns something up**, including issues found and fixed in the same sitting — the value here is the pattern of *what kind of thing breaks and what catches it*, not a ticket queue.

Companion to `docs/validation_results.md`, which tracks measured accuracy. This file tracks correctness and behaviour.

## The single most useful finding

**Almost nothing here was caught by unit tests.** 70 tests pass and passed throughout; the defects below came from running the actual system and looking at the actual graph. Several were invisible by construction — the graph store has no foreign-key enforcement and the renderer drops a dangling edge without a word, so a graph can be badly broken for a viewer while every write "succeeded" and every count looks right.

That is why `scripts/validate_graph_chain.py` exists and why it runs after every graph-writing change. Three separate defects (#12, #15, #16) were found by it and by nothing else.

---

## How to test

```bash
uv run pytest tests/ -q                        # 70 tests
cd ui/frontend && npx tsc -b                   # NOT --noEmit, see #5
uv run scripts/validate_graph_chain.py <case>  # dangling / orphans / reachability / plan relations
```

A real end-to-end run means: open a case through `POST /cases/open`, let the sweeps finish, then run the chain validator. Testing agents in isolation has repeatedly missed defects that only appear on the full path (#11, #12).

---

## Open

| # | Issue | Impact | Notes |
|---|---|---|---|
| O1 | Per-window latency ~15.8s against a 5s window cadence | Graph lags the video substantially; a 90s bounded sweep takes ~5 min wall clock | Dominated by Gemini call time, not writes. Batching already removed the write bottleneck. No fix identified that does not cost accuracy. |
| O2 | Europe PMC grounding — **substantially improved 2026-08-16, not closed** | Was 0 of N complications grounded, so the verification gate could never pass. Now **7 of 17** in a live case, with 7 real evidence edges | Root cause was query construction, not ranking: Europe PMC ANDs every term, so one long query starves the ranker rather than refining it (608 hits at 4 terms → 1 at 10). Fixed with multi-query fanout plus Reciprocal Rank Fusion — see the fanout entry under Fixed. Remaining gap: whether an alert clears the gate now depends on which complication happens to diverge, since ~60% are still ungrounded. `sort=CITED desc` and dropping the open-access filter were both measured and made results *worse*. |

| O4 | `DEFAULT_THRESHOLD=1.7` never re-tuned | Error detection skews toward false negatives | Confusion matrix in `validation_results.md` shows the skew across two separate runs. Flagged since 2026-08-13. |

---

## Fixed

### Graph structure and the chain

**#12 — Perception output rendered as disconnected nodes.**
Every event, phase, snapshot and vitals node floated with no path back to the agent that produced it. Found by looking at the rendered graph. Root cause: nothing ever wrote the edges. Fixed by adding a `succession` edge kind and building a real spine — the agent owns the first activity, each activity follows its predecessor, and events hang off the activity they occurred during. §4.3 already required timestamp ordering; this makes it a graph relation rather than only a sort key.

**#13 — Error Detection wrote `detection` edges to a node that never existed.**
It emitted edges from `phase:{id}` while Perception writes `phase:{id}:{window}`. ReactFlow silently discarded every one, so error nodes floated. Same visible symptom as #12, entirely different cause. Fixed by anchoring to the coordinator node, which the static skeleton guarantees exists.

**#14 — Error Detection wrote duplicate orphaned phase nodes.**
Labelled `"Phase 0 (0:00–0:12)"` — the meaningless ID-based label plan_v2 §13.4 rules out. The guard meant to prevent it looked for `phase:{id}` while Perception writes `phase:{id}:{window}`, so it never matched. Removed entirely; Error Detection has no semantic phase description to offer, as its own docstring said.

**#15 — Snapshot-slot edges written before the slots existed.**
The four `agent → snapshot slot` links were written up front, but a slot node is only created once a window has content for it. Caught by the chain validator. Fixed by having the links ride the same batch that creates the slot — the store assigns `seq` in list order, so the node always lands first.

**#16 — Literature nodes orphaned, then parented to the wrong thing.**
First they had no edges at all; then they hung off the Literature Retrieval agent, making it a large competing hub that buried the sequence a reader follows. Now they hang off the error whose investigation retrieved them, so the flow reads error → literature → complication.

### Reasoning correctness

**#6 — The activity-persistence rule never fired once.**
Implemented as "the same normalized description must appear in two consecutive windows". The model does not repeat itself when an activity is stable — it describes each window's specific moment, so three consecutive windows of one continuous suturing activity returned three different strings. `current_activity` stayed `None` for an entire sweep. §7.6 says *"the change persists"* — it is the **departure from the established activity** that must persist, not one exact wording. The three real descriptions are now regression tests.

**#7 — Vitals flagged a deviation on essentially every window.**
Deviations were measured against the patient's pre-insufflation baseline, but insufflation legitimately raises EtCO2 and airway pressure for the whole case. A vitals node would have been written every window, flooding the graph with exactly the steady-state noise the change-diff design exists to suppress. Now measured against `expected_at(frac)` — the uncomplicated course at that moment. Result: 12 of 55 windows deviate instead of all of them.

**#24 — Corrective action library paraphrased its sources.**
Two entries claimed to invert an OCHRA indicator but quoted it approximately (`"without repositioning"` vs the real `"without progress"`). Caught by a test that asserts every action quotes a real indicator verbatim — written specifically because the library's honesty rests on that claim.

**#26 — One long literature query returned nothing usable.**
Europe PMC is a boolean AND system: every term is a clause the paper must literally contain, so a precise-sounding query eliminates the papers it was meant to find. Measured on one real clinical question: 4 terms → 608 hits, 10 terms → 1 hit, and that survivor was a conference abstract book — the only document dense enough to contain every term somewhere. The agent's own instruction was the cause; it told the model to include mechanism and patient specifics, which is exactly what breaks this API. A `broader_query` fallback existed but only fired on *zero* hits, so 66 tangential results counted as success and it never ran. Replaced with multi-query fanout (3–5 short independent queries, hard 4-content-word cap, synonyms as separate queries) merged by Reciprocal Rank Fusion. Field prefixes verified working; MESH turned out far narrower than expected (68 hits for a central term, top result about women undergoing implant explant) so it is offered as at most one query rather than the default.

**#25 — Five near-identical divergence alerts for one event.**
One error spawned several complications, each produced a proposal, and each proposal independently noticed the same recurrence. Per-proposal deduplication could not catch it since every alert was genuinely the first *for its proposal*. Now also deduplicated by the underlying evidence. 5 → 1 on the following run.

### Wiring and orchestration

**#11 — The event bus was never registered.**
`complication_reasoning.subscribe()` worked and was verified working when called directly, but nothing in `open_case` ever called it — so through the real UI path the agent could never fire at all. Errors appeared and nothing followed. Only visible on the full path; direct agent testing passed.

**#17 — One failed write killed an entire sweep.**
A transient `httpx.ReadError` in a per-window callback propagated out and ended the whole Error Detection sweep on its first window, so no error node was ever written and the entire reasoning chain behind it never ran. docs §10 is explicit that a failed window loses *that window*. Both sweeps now contain per-window failures.

### Infrastructure

**#8 — Eight concurrent writes to one case returned HTTP 503.**
Every write transactionally bumps a single `seq` field on the case document, so concurrent writers exhausted the transaction's five attempts. This is the normal operating condition under the documented concurrency model, not an edge case. Fixed with a per-case write lock plus retry-with-backoff.

**#9 — Write throughput ~1.2s per write, serialized.**
A perception window emitting ten patches cost ~10s against a 5s cadence. Added a batch endpoint applying N items in one transaction with consecutive seq values. Measured 15.08s → 2.98s for ten writes.

**#10 — Stale pooled connections after the httpx swap.**
Uvicorn closes idle keep-alive connections after 5s; writers here can go 15s+ between calls, so httpx handed back sockets the server had already closed. The previous `requests` path never hit this because it opens a fresh connection per call — the pooling that made writes faster is what exposed it. Fixed with a shorter keepalive expiry than the server's plus transport-level retry, which is safe because the request never arrived and cannot have duplicated a write.

**#5 — The frontend typecheck was checking nothing.**
`ui/frontend/tsconfig.json` is `{"files": [], "references": [...]}`, so `npx tsc --noEmit` resolves zero input files and always exits 0. Every "typecheck clean" record before 2026-08-16 was a false signal. The correct command is **`npx tsc -b`**, which surfaced 15 real pre-existing errors on first run. Corrected in `validation_results.md`.

**Firestore database not persisted in `.env`.**
`FIRESTORE_DATABASE` defaulted to `(default)`, which does not exist for this project, causing silent 500s on every restart where the var was not exported. Now in `.env`.

### UI

**#18 — The trigger node appeared to be missing.**
It was always written with all its edges. ReactFlow's `fitView` prop runs on mount, when the graph is still empty, so it fitted nothing and every node arriving afterwards landed outside the viewport — and the trigger sits at the far left as the layout root, so it was first off screen. Now fits exactly once, when the first nodes arrive. Deliberately not a re-fit on every change; that was removed on purpose because it yanked the viewport mid-inspection.

**#19 — Errors that looked identical behaved differently.**
The node displayed *confidence*, but confidence is not what gates downstream reasoning — severity is, and the two are unrelated. An error at 95% confidence can be medium while another at 90% is high. Severity now drives the outline colour and appears on the node.

**#20 — Missing complications looked like dropped steps.**
The more common reason for a missing complication turned out not to be severity at all: a category already reasoned about in the current phase is skipped, which is the documented idempotency rule but was completely invisible. Every error now carries `complication_status` — `reasoned`, `already_reasoned`, `below_severity_threshold`, or `none_warranted`.

**#21 — Complication edges drawn solid.**
Solid reads as "this happened". A complication is a reasoned possibility, so it is now dashed, consistent with `prediction` and `proposal` in §4.2.

### External API integration

**#22 — Literature queries returned zero results.**
Europe PMC requires every query term, so the agent's precise ten-word query matched literally nothing while an eight-word one returned four results. Fixed by having the agent supply its *own* broader fallback — only it knows which terms carry the clinical question, so a deterministic truncation would strip blindly.

**#23 — Wrong field name and raw markup.**
The API returns `snippet`, not `abstract`, so the model was receiving titles with no text at all. Titles and snippets also carry HTML entities and inline tags that were landing verbatim in prompts and node labels. Both fixed at the retrieval boundary.

### Cloud Run deployment (2026-08-23)

First real deploy of `state_service`, `orchestrator_service`, and the frontend to Cloud Run. Every issue below was silent or misleading at first — no exception, or an error message pointing at the wrong layer — found only by watching real graph state after a real deployed case, the same lesson as #11/#17 one level up the stack.

**#27 — Video file silently excluded from every deploy, despite a correct `.dockerignore`.**
`gcloud run deploy --source` uploads to Cloud Build using `.gcloudignore` — and when that file doesn't exist, gcloud auto-generates one from `.gitignore`. This repo's `.gitignore` excludes `data/video/*` (correctly, for git — an 878MB binary has no business in version history). Editing `.dockerignore` first had zero effect: that file only controls what the Dockerfile copies from an *already-uploaded* source into the image, and the video never reached the upload at all. Two separate ignore-file checks at two different stages; the real gate was one stage earlier than the symptom pointed. Fixed with an explicit `.gcloudignore` that does not inherit `.gitignore`'s git-history-hygiene rule for this unrelated question ("what does the running app need").

**#28 — Orchestrator's vision agents need the video on local disk; `state_service` does not.**
`cv2.VideoCapture` (`tools/video_utils.py`, real frame extraction) only opens local file paths, never a GCS URI — unlike `state_service`'s browser-facing stream (`gcs_video.py`), which reads GCS byte-ranges directly and never touches local disk. Baking the video into the shared Dockerfile fixes the orchestrator but also ships the same ~878MB, completely unused, inside `state_service`'s image, since both services build from one Dockerfile. Accepted for now, not the ideal fix — see the retrospective note at the bottom of this section.

**#29 — Two different real URLs for the same Cloud Run service, only one allow-listed for CORS.**
Cloud Run gives every service both a hash-based URL (`*-bfa6iiuaia-uc.a.run.app`) and a project-number-based one (`*-518946358970.us-central1.run.app`) — both serve identical content, but a browser treats them as two entirely different origins. `STATE_SERVICE_CORS_ORIGINS`/`ORCHESTRATOR_SERVICE_CORS_ORIGINS` were set to only the first (the one `gcloud run services describe --format='value(status.url)'` returns); loading the app via the second got a real `400` from Starlette's own CORS preflight rejection, surfaced by the browser as a bare "CORS error" with no indication it was just an origin mismatch. Fixed by allow-listing both, comma-separated — which itself needed `gcloud`'s `^@^` delimiter-override syntax, since `--update-env-vars` normally uses commas to separate *different* variables, not values within one.

**#30 — Cloud Run's default CPU throttling silently stalled background sweep work.**
`/cases/open` returns `200 OK` immediately and does the real sweep afterward via FastAPI `BackgroundTasks`, deliberately, so the frontend isn't blocked. Cloud Run's default (CPU throttled between requests) only guarantees CPU while a request is actively being handled — once the response was sent, the background task was starved with zero further log output, not even an exception. A case would sit forever at its initial agent-registration nodes with no error signal anywhere to point at. Fixed with `--no-cpu-throttling`. Load-bearing for any Cloud Run service that starts real work it doesn't wait on before responding — Cloud Run's default assumption (nothing meaningful happens outside a request/response cycle) is false for this architecture specifically.

**#31 — 512Mi default memory OOM-killed the orchestrator mid-sweep, silently.**
Real log line: `Memory limit of 512 MiB exceeded with 512 MiB used`. Cloud Run killed the instance and started a fresh one to keep serving traffic — the fresh instance has no knowledge of the in-flight sweep that died with the old one, so the case stalls permanently with no error ever reaching the graph. Same visible symptom as #30 (silent stall at N nodes), completely different mechanism (a real kill, not starvation) — found only by reading Cloud Run's own infra-level logs, not the app's. Decoded video frames plus concurrent multimodal Gemini payloads need real headroom; fixed with `--memory=2Gi --cpu=2`, verified against a live 8+-minute run producing 287 real graph nodes with zero stalls.

**#32 — `state_service`'s default 300s request timeout cut its own SSE stream.**
Only the orchestrator's deploy set `--timeout=3600`. `state_service`'s long-lived `GET /state/{case_id}/stream` connection is exactly the same shape of long-running request and was left on Cloud Run's 300s default. Confirmed via a real log line (`"Truncated response body. Usually implies that the request timed out..."`). Fixed with the same `--timeout=3600`.

**Retrospective — the ideal fix for #28, not yet implemented.** Baking one fixed video into the image is the wrong shape: it doesn't generalize to a second video without a rebuild, and it wastes ~878MB in the one service that never uses it. The correct pattern (standard for "container needs local file access to something durably in object storage") is lazy download-on-demand: check a local cache path, download from GCS once on a miss, serve local reads exactly as today after that. One real gotcha if this is ever built: Cloud Run's default writable filesystem (`/tmp`) is memory-backed, so a naive cache would eat directly into the same memory budget #31 just fixed — needs either a deliberately larger `--memory`, or Cloud Run gen2's real (non-memory) mounted volume support for scratch space.

**Process lesson.** Every one of #29–#32 is a different Cloud Run *default* (CORS origin list, CPU allocation, memory, request timeout) that is individually reasonable and was wrong for this specific app's shape — long-lived streams, background work outliving its triggering request, real per-request memory use. Hitting one wrong default is not evidence the others are fine; the right response the first time a Cloud Run default causes a silent failure is to check all of them against how *this* app actually behaves, not patch the one that happened to surface first.

---

## Process issues worth remembering

**A test fixture put a fabricated citation into a public FHIR record (2026-08-16).**
Verifying the verification gate's *pass* path required a chain the gate would accept, and real runs never produce one because complications keep coming back `evidence_backed=False`. The chain was written by hand — including a literature node with an invented title, journal and year (`"Bladder neck reconstruction outcomes after RARP", J Endourol 2024`) pointed at an arbitrary article id. That alert was written to the public HAPI server and then offered as something to demo.

Two things make this worse than it first looks. The invented id resolved to a **real but entirely unrelated paper** (gastric antrum anatomy), so the citation looked plausible and survived a naive URL check — exactly the failure mode `evidence_backed` and the gate exist to catch. And it happened while testing the component whose only job is refusing ungrounded claims.

Re-verified with a genuinely retrieved paper (`Communication/137353000`, EXT_ID 42195151, confirmed hitCount=1 and on-topic). **Rule going forward: a test fixture may hand-build graph STRUCTURE, but any content that would be presented as evidence must be really retrieved.** Structure is scaffolding; a citation is a claim.

**Backgrounded restarts queued behind long commands killed live test runs twice.** A restart command chained after a two-minute pytest fired long after I had already restarted manually, killing the servers mid-sweep and producing a "stalled" case that looked like a code defect. Restarts now go in their own command.

**Testing agents in isolation is not sufficient.** #11 and #17 both passed direct invocation and both made the system completely non-functional through the UI. A real case opened through the orchestrator is the only test that would have caught either.
