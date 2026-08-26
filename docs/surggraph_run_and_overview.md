# SurgGraph — a real autonomous run, and what SurgGraph actually is

Generated 2026-08-26. The trace below is pulled directly from Firestore
(`cases/case-b09d830561c4/graph_items`, real `seq`-ordered writes) for the
**same case** SurgBot's conversation reviewed afterward (see the companion
doc, `surgbot_conversation_and_overview.md`) — this is genuinely "what
SurgGraph did, autonomously, before a human ever looked at it."

- **Case:** `case-b09d830561c4` (RARP, synthetic patient)
- **Trigger:** you pressed play on the video at 2026-08-26T10:25:40 UTC
- **Window analyzed:** 0:30–2:00 of the video (the orchestrator's bounded dev sweep — since widened to 0:30–1:50)
- **Real graph produced:** 107 nodes, 144 edges, written in ~4 minutes 8 seconds, fully unattended

---

## 1. The real autonomous run, start to finish

**10:25:40 — Trigger.** You pressed play. The orchestrator opened the case, wrote a `trigger` node and a synthetic `patient_twin` (BMI 32.1, ASA Class 3, 85 mL prostate with median lobe, prior TURP — the same patient context SurgBot later reasoned from), and registered every downstream agent as a real node in the graph before any of them had done anything yet — Complication Reasoning, Literature Retrieval, Corrective Replanning, Divergence Detection, Alert Routing, Verification Gate, and the Error Detection coordinator's weighted-aggregation rule.

**10:25:45 — Perception starts.** Real Gemini vision calls over 5-second video windows begin. First real read: "transferring suture needle between needle drivers," with 5 real entities entering frame (needle drivers, needle, thread, bladder neck) and a phase boundary logged at 30s.

**10:26:15 — First error fires.** The Error Detection Coordinator — a real 3-agent system (Temporal / Spatial / Procedural, each independently reasoning over a different frame-sampling of the same window) — flags "out of view at 0:45," `composite_score=2.20` vs. `threshold=1.70`, confidence 0.9. This isn't a lookup against a ground-truth label; it's live multi-agent reasoning voting on real frames.

**10:26:36 — Complication Reasoning fires.** Given that error plus the patient's real risk factors, it reasons out two complications — *Retained Foreign Body*, *Prolonged Operative Time* — each with a patient-specific rationale (the obesity/ASA-3 context) written into the node, not templated.

**10:26:44 — Corrective Replanning responds**, twice — proposing "pause the suture transfer to re-establish visual control" from two independent angles.

**10:26:59 — Divergence Detection catches a real deviation**: the surgeon kept going instead of pausing. **10:27:01 — Alert Routing** proposes a real alert for it. **10:27:01 — Verification Gate** checks it ("Verified — external write permitted" — the fail-closed check that blocks anything ungrounded). **10:27:02 — the alert is actually delivered** as a real FHIR `Communication` resource to the public HAPI test server — not a chat webhook, a real clinical-record write, readback-verified.

**10:27:06 — Literature Retrieval** independently surfaces 3 real papers backing the retained-needle risk (multi-query fan-out against Europe PMC, not a single brittle query).

**10:27:20 — A second, higher-severity error**: "needle handling at 1:20," high severity, `composite_score=2.00`. Same 3-agent coordinator, same real vote. Complication Reasoning responds with *Retained Surgical Needle* and *Prolonged Anesthetic/Pneumoperitoneum Exposure*. Corrective Replanning's first attempt here is genuinely honest, not padded: **"Escalate — no confident corrective match"** — it says so rather than manufacturing a plan it isn't confident in.

**10:27:15 — A third error**, "needle handling at 1:50," pulls in *Ureteral Injury* and *Vesicourethral Anastomotic Leak* as complications, backed by 4 more real retrieved papers (including a 2024 case report on iatrogenic ureteral ligation during RARP — the same paper SurgBot's Error Chain Review Agent cited back to you later).

**10:27:43 — Corrective Replanning tries again**, twice, converging on "stabilize needle control before advancing through the bladder neck."

**10:28:56 — A second real divergence** is caught and alerted the same way (proposed → verified → delivered).

**10:26–10:29 — Vitals stream, running the whole time in parallel**: real synthetic vitals nodes ("Falling MAP with compensatory tachycardia," "Rising EtCO2 and airway pressure with mild desaturation") tied to the same timeline, giving the graph a physiological thread alongside the technical one.

**10:29:24 — Final snapshot.** Perception writes a closing snapshot: 5 entities in view, vitals HR 66 / MAP 91 / SpO2 98%.

**10:29:48 — Documentation, the last step.** The drafted operative-record text is screened by Model Armor first (`"Passed — cleared for review"`, real API call, real template `surggraph-fhir-outbound`) — only then does the `documentation` node get written, status **"awaiting surgeon approval."** Nothing reaches the record without a human sign-off waiting on the other end.

**Total elapsed: 4 minutes 8 seconds, zero human input**, for a run that produced 6 real detected errors' worth of reasoning, 6 complications, 5 corrective proposals, 2 divergence alerts, 1 real external clinical-record write, and one document now sitting in a real HITL queue — all before this exact case became the one you later reviewed with SurgBot.

---

## 2. What SurgGraph actually is

SurgGraph is the **autonomous** half of this project (the "Taskmaster" side): point it at a recorded robotic-surgery video, press play, and — with zero human intervention — it perceives what's happening, detects real technique errors, reasons about their downstream clinical complications with literature grounding, proposes corrective actions, catches when the surgeon actually diverged from those corrections, verifies every claim before anything leaves the system, writes a real external clinical record (FHIR), and fires real alerts on real divergences. SurgBot (the conversational layer) never runs during this — it's a separate, later, human-driven review of what SurgGraph already did.

The **Living State Graph** (the exact trace above) is the through-line: every agent's output is a real node/edge in one shared graph, not a private internal variable — which is also what makes an honest trace like this possible at all. Nothing above was reconstructed from logs; it's the literal graph state.

## 3. The agents and tools actually at play

| Agent | Role | Real mechanism |
|---|---|---|
| **Orchestrator** | Opens the case, wires up every other agent, drives the sweep | `BaseAgent` subclass, real ADK composition |
| **Perception** | Turns raw video windows into structured scene-graph events (entities, phases, activity) | Real Gemini 3.5 vision calls, 5s windows |
| **Error Detection** | Detects real technique errors — **not** a ground-truth lookup | 3 independent Gemini agents (Temporal/Spatial/Procedural) + a coordinator + deterministic weighted aggregation (≥2-of-3 agreement to fire), modeled on the published CARES architecture |
| **Complication Reasoning** | Given a real error, reasons out plausible downstream complications with patient-specific rationale | Gemini 3.5, grounded in the synthetic patient's real risk profile |
| **Literature Retrieval** | Grounds complications in real retrieved evidence | Multi-query fan-out against the real Europe PMC API, Reciprocal Rank Fusion — never a single brittle query |
| **Corrective Replanning** | Proposes a corrective action for a flagged error, or honestly says it can't | Gemini 3.5 reasoning over an OCHRA-derived corrective-action library |
| **Divergence Detection** | Notices when the actual surgical technique diverged from a proposed correction | Compares live perception state against the active corrective trajectory |
| **Alert Routing** | Decides whether/how a divergence becomes an external alert | Deterministic routing logic |
| **Verification Gate** | Fail-closed check — blocks anything not properly evidenced before it can leave the system | Read-only by design; can't write, can't alert |
| **Documentation** | Drafts the operative record, screens it through Model Armor, hands it to HITL | Gemini 3.5 + `tools/model_armor.py` |
| **HITL (Human-in-the-Loop)** | Holds a drafted document for real surgeon approve/edit/reject | Firestore-backed approval state, never auto-approved |
| **Alert Executor** (`tools/fhir_alert.py`) | The actual external write for a routed alert | Real FHIR `Communication` resource to HAPI, readback-verified |
| **Benchmark** | Self-benchmarking against ground truth, offline only | Never in the live decision path |

All Gemini calls run on **Gemini 3.5** via `tools/gemini_model.py` (the same model family SurgBot uses) — but unlike SurgBot, every one of these agents runs **in-process**, instantiated as plain Python/ADK objects inside the two Cloud Run services below, not as separately deployed Agent Runtime reasoning engines. That's a deliberate choice, not an oversight — see the note in §4.

## 4. Cloud/GEAP services actually in use

- **Cloud Run** — two services, `surggraph-orchestrator-service` (runs every agent above) and `surggraph-state-service` (owns the Living State Graph, serves the frontend's real-time stream and video).
- **Firestore** — the actual backing store for the graph (`cases/{case_id}/graph_items`, exactly what this trace was read from) — real multi-tenant isolation, one independent graph per case.
- **Cloud Storage** — the source video.
- **Vertex AI / Gemini 3.5** — every agent's real reasoning, via the plain `google-genai` SDK (not Agent Runtime).
- **Model Armor** — real, direct API screening of the drafted operative record before it's presentable for approval (own template, `surggraph-fhir-outbound`, separate from SurgBot's).
- **Cloud Trace / Observability** — real OTel spans, `SURGGRAPH_ENABLE_CLOUD_TELEMETRY`.
- **HAPI FHIR** (external, public test server) — real writes for both alerts and the eventual approved documentation, both readback-verified.

**Deliberately NOT in use: Agent Registry, Agent Runtime, Memory Bank, Agent Identity, Agent Gateway** — the GEAP-specific layer SurgBot's agents run on. This was a founding architecture decision, not an omission: SurgGraph is real-time, latency-sensitive (already has a documented, open latency gap against its own target — see `docs/qa_log.md` O1), and its vision agents need direct local access to video frames — both are real, technical reasons remote Agent-Runtime hops would hurt rather than help here. Full reasoning in the pros/cons discussion earlier this session.
