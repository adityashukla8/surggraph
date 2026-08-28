# SurgOS — Visual Workflow Reference

Diagram source for the submission. `liveapi-488810` · `us-central1`

---

## 1. The Loop

```mermaid
flowchart LR
    subgraph L1["① SurgGraph — ACTS"]
        A1["12 in-process ADK agents<br/>─────────<br/>Cloud Run + Vertex AI"]
    end
    subgraph L2["② SurgBot — REVIEWS"]
        A2["Root + 4 subagents<br/>─────────<br/>GEAP Agent Runtime"]
    end
    subgraph L3["③ Learning Loop — TEACHES"]
        A3["Routing + Memory Bank<br/>─────────<br/>similarity retrieval"]
    end

    VID(["🎥 Surgical video<br/>+ patient twin"]) ==> A1
    A1 ==> FHIR(["📄 FHIR record + alert<br/>REAL external EHR"])
    A1 -.->|completed case| A2
    VOICE(["🎙️ Surgeon voice"]) ==> A2
    A2 ==> DOC(["✅ Approved review"])
    A2 -.->|on approval ONLY| A3
    A3 ==>|advisory context<br/>at inference| A1

    style L1 fill:#1e3a5f,color:#fff
    style L2 fill:#2d5016,color:#fff
    style L3 fill:#5c3317,color:#fff
```

---

## 2. ① SurgGraph — Autonomous Pipeline

```mermaid
flowchart TD
    IN(["POST /cases/open"]) --> HIER["Static hierarchy + Gemini warm-up"]
    HIER --> SUB["Register 4 event-bus subscriptions"]
    SUB --> FORK{{"asyncio.gather — CONCURRENT"}}

    FORK --> ED["<b>Error Detection Coordinator</b><br/><i>BaseAgent · not an LLM</i>"]
    FORK --> PE["<b>Perception</b><br/><i>Gemini 3.5 · vision · tools=[]</i>"]

    ED --> R1["<b>temporal</b><br/><i>Gemini 3.5</i>"]
    ED --> R2["<b>spatial</b><br/><i>Gemini 3.5</i>"]
    ED --> R3["<b>procedural</b><br/><i>Gemini 3.5</i>"]
    R1 & R2 & R3 --> AGG["<b>Weighted Aggregation</b><br/><i>DETERMINISTIC · ≥2-of-3 · α · thr 1.7</i>"]
    AGG --> SEV["<b>Severity scoring</b><br/><i>DETERMINISTIC</i>"]
    SEV --> BUS(("event bus<br/>in-process"))

    BUS -->|error<br/>severity≥med| CR["<b>Complication Reasoning</b><br/><i>Gemini 3.5 ×2</i>"]
    CR --> LIT["<b>Literature Retrieval</b><br/><i>DETERMINISTIC · 3 APIs + RRF</i>"]
    LIT -.->|EuropePMC · PubMed<br/>SemanticScholar| CR
    CR -->|complication| RP["<b>Corrective Replanning</b><br/><i>Gemini 3.5 · bounded library</i>"]
    RP -->|corrective_trajectory| DD["<b>Divergence Detection</b><br/><i>DETERMINISTIC first · Gemini if ambiguous</i>"]
    DD -->|divergence_alert| AR["<b>Alert Routing</b><br/><i>no reasoning call</i>"]

    AR --> GATE{{"<b>VERIFICATION GATE</b><br/><i>NOT an LLM · fail-closed · 11 checks</i>"}}
    GATE -->|✅ pass| ALERT(["FHIR Communication<br/>real write + readback"])
    GATE -->|🚫 block| BLK(["verification_block<br/>visible refusal"])

    PE --> DRAIN["bus.drain 70s"]
    ED --> DRAIN
    DRAIN --> DN["<b>Documentation</b><br/><i>🩺 MedGemma 4B — medical-domain<br/>ONLY model for this step, no fallback</i>"]
    DN --> ARM{{"Model Armor<br/>output screen"}}
    ARM --> H2

    H2["👤 <b>HITL — approve / edit / reject</b>"] --> GATE2{{"Gate + Model Armor<br/>re-screen edited text"}}
    GATE2 --> FHIR(["FHIR DocumentReference<br/>readback-verified"])

    RP -.-> H1["👤 <b>HITL — acknowledge / dismiss</b>"]
    H1 -.->|advisory| DD

    style AGG fill:#4a4a4a,color:#fff
    style SEV fill:#4a4a4a,color:#fff
    style LIT fill:#4a4a4a,color:#fff
    style AR fill:#4a4a4a,color:#fff
    style ED fill:#4a4a4a,color:#fff
    style GATE fill:#8b0000,color:#fff
    style GATE2 fill:#8b0000,color:#fff
    style H1 fill:#b8860b,color:#fff
    style H2 fill:#b8860b,color:#fff
    style FHIR fill:#006400,color:#fff
    style ALERT fill:#006400,color:#fff
```

**Legend** ▪ ⬛ grey = deterministic Python (not an LLM) ▪ 🟥 red = fail-closed gate ▪ 🟨 amber = human ▪ 🟩 green = real external write

| IN | STATE | OUT |
|---|---|---|
| SAR-RARP50 video · opaque phase IDs · synthetic patient twin | Living State Graph — 19 node types, 13 edge kinds, Firestore transactions, SSE fan-out | FHIR `DocumentReference` + `Communication` → real HAPI server |

> ⚠️ **12 agents run.** Anticipation · Scene Graph Builder · Benchmark exist but are **not dispatched**.
>
> 🩺 **MedGemma owns the Documentation step.** The operative record — the one artifact that reaches a
> real external EHR — is written by a **medical-domain model**, not a general one. Self-deployed
> `medgemma-4b-it` on an always-warm L4 endpoint. **No Gemini fallback**: a failure surfaces as a real
> failure, because a record whose provenance says *medical model* must not quietly be written by a
> general one. Provenance is recorded on the node (`drafted_by_model`), so the claim is checkable in
> the graph rather than only asserted.
>
> Measured head-to-head on the real production instruction + real case slice, scored on the five
> framing properties this step exists to enforce:
>
> | | latency | schema | framing checks |
> |---|---|---|---|
> | **MedGemma 4B** | **10.65s** | valid | **5/5** |
> | Gemini 3.5 Flash | 17.04s | valid | 5/5 |
>
> Not a quality concession — faster here, same honesty contract.

---

## 3. ② SurgBot — Conversational Review

```mermaid
flowchart TD
    BR(["🎙️ Browser · push-to-talk · PCM16"]) <-->|WebSocket| RELAY

    subgraph CRUN["Cloud Run — surgbot relay"]
        RELAY["WS relay"]
        STT["<b>MedASR</b><br/><i>Vertex endpoint</i>"]
        TTS["<b>Chirp 3 HD</b><br/><i>streaming PCM 24k</i>"]
    end

    RELAY --> STT --> ARMOR
    ARMOR{{"<b>MODEL ARMOR</b><br/>screen_user_input<br/><i>FAIL CLOSED</i>"}}
    ARMOR -->|🚫 blocked| STOP(["never reaches Gemini"])
    ARMOR -->|✅ pass| ROOT

    subgraph GEAP["GEAP Agent Runtime"]
        ROOT["<b>surgbot-root-agent</b><br/><i>Gemini 3.5 · 9 REAL TOOLS</i><br/>service_account"]
        SA1["<b>error_chain_reviewer</b><br/><i>AGENT_IDENTITY</i>"]
        SA2["<b>synthesis</b><br/><i>AGENT_IDENTITY</i>"]
        SA3["<b>pattern_insight</b><br/><i>AGENT_IDENTITY</i>"]
        SA4["<b>feedback_router</b><br/><i>AGENT_IDENTITY</i>"]
    end

    ROOT -->|async_stream_query| SA1 & SA2 & SA3 & SA4
    ROOT --> TTS --> BR
    SA2 -.->|parent engine| MB[("Memory Bank")]
    ROOT -.->|read| GRAPH[("Living State Graph")]
    SA2 --> ARM2{{"Model Armor<br/>output screen"}}
    ARM2 --> H["👤 <b>HITL — approve / edit / reject</b>"]

    style ARMOR fill:#8b0000,color:#fff
    style ARM2 fill:#8b0000,color:#fff
    style H fill:#b8860b,color:#fff
    style GEAP fill:#2d5016,color:#fff
```

### 6 phases → tools → subagents

```mermaid
flowchart LR
    P1["1 · Case framing"] --> P2["2 · Phase walkthrough"] --> P3["3 · Error review"] --> P4["4 · Proposal review"] --> P5["5 · Synthesis"] --> P6["6 · Cross-session"]
    P1 -.- T1["list_accessible_cases<br/>load_case_graph"]
    P2 -.- T2["get_phase_detail"]
    P3 -.- T3["review_error_chain<br/>→ error_chain_reviewer"]
    P4 -.- T4["review_proposal_divergence"]
    P5 -.- T5["draft_review_document<br/>→ synthesis"]
    P6 -.- T6["retrieve_reviewer_patterns<br/>→ pattern_insight"]
```

> 🔑 **The inversion:** every other agent is `tools=[]`. SurgBot root is the *only* agent with real tools — a surgeon-driven conversation can't be pre-sliced. Both conventions have tests enforcing them.

---

## 4. ③ Learning Loop

```mermaid
flowchart LR
    APP(["👤 Review approved"]) --> CLS{{"Classify"}}
    CLS -->|node-anchored<br/>+ verdict| OBS["<b>observation</b><br/><i>routed in code · NO LLM</i>"]
    CLS -->|free text| RTR["<b>feedback_router</b><br/><i>Gemini 3.5</i>"]
    OBS & RTR --> ARM{{"Model Armor<br/><i>human text → LLM prompt</i>"}}
    ARM -->|🚫| FS[("Firestore only<br/>memory_written=false")]
    ARM -->|✅| FS2[("<b>Firestore</b><br/>system of record")]
    FS2 --> MB[("<b>Memory Bank</b><br/>scoped by target_agent")]
    MB --> BLK["feedback_block()<br/><i>300s cache · fail-soft · bounded</i>"]
    BLK --> C1["literature_retrieval"]
    BLK --> C2["complication_reasoning"]
    BLK --> C3["corrective_replanning"]
    BLK --> C4["divergence_detection"]

    style ARM fill:#8b0000,color:#fff
    style APP fill:#b8860b,color:#fff
```

| Routing | → agent | v1 |
|---|---|---|
| `divergence_alert` | divergence_detection | ✅ |
| `complication` | complication_reasoning | ✅ |
| `literature_evidence` | literature_retrieval | ✅ |
| `corrective_trajectory` | corrective_replanning | ✅ |
| `error` | error_detection | ⚠️ stored, not consumed |

**Guardrails** — advisory text only · *"NOT ground truth"* · forbids suppressing findings · **gate untouched** · approval-gated · fail-soft to `""`

---

## 5. Hosting — Why Different Runtimes

```mermaid
flowchart TB
    subgraph CR["☁️ Cloud Run + Vertex AI — SurgGraph"]
        direction TB
        C1["🔁 <b>High call volume</b><br/>tight per-window loops"]
        C2["⚡ <b>In-process event bus</b><br/>splitting breaks the invariant"]
        C3["🎞️ <b>Local frame access</b><br/>cv2 on disk, no GCS fallback"]
        C4["🔒 <b>Agent Identity 401s</b><br/>on outbound GCP API calls"]
    end
    subgraph AR["🤖 GEAP Agent Runtime — SurgBot"]
        direction TB
        A1["💬 <b>Low call volume</b><br/>one turn per few seconds"]
        A2["🔐 <b>Agent Identity works</b><br/>tool-free subagents"]
        A3["🧠 <b>Memory Bank native</b><br/>substrate of Layer 3"]
        A4["🔗 <b>Managed sessions</b><br/>session_id continuity"]
    end
    style CR fill:#1e3a5f,color:#fff
    style AR fill:#2d5016,color:#fff
```

> **Not an inability — a measured choice.** SurgBot *is* on Agent Runtime, proving capability.
> Agent Registry needs neither: **both Cloud Run services are registered.**

---

## 6. GCP Services

```mermaid
flowchart TB
    subgraph COMPUTE["Compute"]
        RUN["<b>Cloud Run</b> ×4<br/>state · orchestrator · surgbot · frontend"]
        AE["<b>GEAP Agent Runtime</b> ×5"]
    end
    subgraph AI["AI / ML"]
        VX["<b>Vertex AI</b><br/>Gemini 3.5 Flash · MedGemma · MedASR"]
        SP["<b>Speech-to-Text / TTS</b><br/>Chirp 3 · Chirp 3 HD"]
    end
    subgraph GOV["Govern"]
        REG["<b>Agent Registry</b> ×6"]
        MA["<b>Model Armor</b><br/>1 template · BOTH directions"]
        MEM["<b>Memory Bank</b>"]
    end
    subgraph DATA["Data"]
        FS[("<b>Firestore</b><br/>graph · reviews · feedback")]
        GCS[("<b>Cloud Storage</b><br/>video · annotations")]
    end
    subgraph OBS["Observe / Ship"]
        TR["<b>Cloud Trace</b> OTel GenAI"]
        LG["<b>Logging + Monitoring</b>"]
        CB["<b>Cloud Build</b> → push to main"]
    end
```

### Identity separation

```mermaid
flowchart LR
    CI["<b>surggraph-cicd</b><br/>deploy ✅ · data 🚫"]
    RT["<b>surggraph-runtime</b><br/>data ✅ · deploy 🚫"]
    FE["<b>surggraph-frontend-sa</b><br/><i>ZERO roles</i>"]
    SA["<b>4 subagents</b><br/>SPIFFE · no long-lived key"]
    style CI fill:#1e3a5f,color:#fff
    style RT fill:#2d5016,color:#fff
    style FE fill:#4a4a4a,color:#fff
    style SA fill:#5c3317,color:#fff
```

> Compromised agent can't deploy · compromised build can't read data · frontend can do neither.

---

## 7. Deterministic vs LLM

```mermaid
flowchart LR
    subgraph DET["⬛ Deterministic Python"]
        D["Verification Gate<br/>Aggregation · Severity<br/>Literature RRF · Alert Routing<br/>Divergence 1st pass"]
    end
    subgraph LLM["🟦 Gemini 3.5 — general reasoning"]
        L["Perception · 3 error roles<br/>Complication Reasoning<br/>Corrective Replanning<br/>SurgBot"]
    end
    subgraph MED["🩺 MedGemma 4B — medical domain"]
        M["Documentation<br/><i>writes the operative record</i>"]
    end
    style DET fill:#4a4a4a,color:#fff
    style LLM fill:#1e3a5f,color:#fff
    style MED fill:#6b2d5c,color:#fff
```

**Model choice is per-step, not global:** general reasoning on Gemini, the medical-writing step on a
medical model, speech on MedASR. Three purpose-built models, each where it's actually the right tool.

> **The rule:** an LLM reasons over evidence. It never does arithmetic, never gates safety, never receives the answer key.

---

## 8. Judging Criteria

| Criterion | % | Evidence |
|---|---|---|
| **Innovation & Utility** | 40 | Autonomous pipeline → **real external EHR write**, readback-verified |
| **Architectural Discipline** | 30 | Deterministic/LLM boundary · 3-way identity split · evidence-based runtime choice · graph state model |
| **Demo & Production Readiness** | 30 | Live chain: detect → cite → gate → FHIR → readback + visible GCP proof |

✅ Gemini 3.5 via Vertex AI ✅ Google ADK ✅ Cloud Run + Firestore + GCS ✅ built in window
**→ Track 1 (Taskmaster).** Track 3 requires Agent Gateway — not implemented.

---

## 9. Disclose

| Limitation | Note |
|---|---|
| ~15.8s/window vs 5s target | **lags real time ~3×** — align homepage copy |
| 7 of 17 complications grounded | improved, not closed |
| Threshold 1.7 never re-tuned | skews false-negative |
| All Cloud Run public | `allUsers` invoker |
| Agent Gateway absent | Track 3 gap |
| Single-video priors | generalization unverified |

**Not a validated clinical model** — a literature-grounded hypothesis generator.

---

## 10. Design Notes

1. **Hero** = §1 loop. Make the ③→① arrow dominant — it's what makes this one system.
2. Colour-code §2 by **deterministic vs LLM** — the differentiator.
3. Draw Model Armor as a **gate**, not a box.
4. Three easy mistakes: showing 15 agents (12 run) · feedback loop as decorative · omitting deterministic parts.
