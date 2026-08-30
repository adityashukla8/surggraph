import { OrchestratorFlow } from "./OrchestratorFlow";

// Every card carries a `kind` and a `model` alongside its descriptive tags.
// The distinction is real and load-bearing, not cosmetic: "Agent" means an LLM
// genuinely reasons at that step, "Tool" means a real capability is invoked
// with no model involved, "Deterministic" means code decides on purpose —
// consensus arithmetic and the fail-closed gate are things a model should
// never be doing — and "HITL" is a point where a human, not a model, decides.
type AgentKind = "Agent" | "Tool" | "Deterministic" | "HITL";

interface AgentCard {
  num: string;
  key: string;
  name: string;
  kind: AgentKind;
  model: string;
  desc: string;
  tags: string[];
}

const GEMINI = "Gemini 3.5 Flash";
const NO_LLM = "No LLM";
const HUMAN = "Human";

// Listed in real pipeline order, so reading the grid left-to-right is reading
// the order a case actually moves through.
const SURGGRAPH_AGENTS: AgentCard[] = [
  {
    num: "01",
    key: "perception_agent",
    name: "Perception",
    kind: "Agent",
    model: GEMINI,
    desc: "One Gemini call per ~5s window over the case video, feeding a deterministic change-diff layer that decides what the graph actually hears about — entities, relations, activity progression.",
    tags: ["5s windows", "change-diff + debounce", "entity registry"],
  },
  {
    num: "02",
    key: "error_detection_temporal",
    name: "Error Detection — Temporal",
    kind: "Agent",
    model: GEMINI,
    desc: "Reasons over motion across the window — hesitation, retries, multi-attempt patterns. One of three independent perspectives on the same frames, each looking for something different.",
    tags: ["3-agent consensus", "OCHRA-grounded", "motion & hesitation"],
  },
  {
    num: "03",
    key: "error_detection_spatial",
    name: "Error Detection — Spatial",
    kind: "Agent",
    model: GEMINI,
    desc: "Judges instrument and anatomical positioning at native resolution — where things sit relative to each other, and what left the field of view.",
    tags: ["3-agent consensus", "OCHRA-grounded", "native-res frames"],
  },
  {
    num: "04",
    key: "error_detection_procedural",
    name: "Error Detection — Procedural",
    kind: "Agent",
    model: GEMINI,
    desc: "Compares observed technique against expected protocol for the current step. Its vote is weighted against the other two, and no single perspective can raise an error alone.",
    tags: ["3-agent consensus", "OCHRA-grounded", "weighted aggregation"],
  },
  {
    num: "05",
    key: "complication_reasoning",
    name: "Complication Reasoning",
    kind: "Agent",
    model: GEMINI,
    desc: "Event-driven off error nodes above a severity threshold — formulates a literature query from live case context, reasons over the retrieved abstracts, and writes complications linked back to their evidence.",
    tags: ["event-driven", "literature-grounded", "causal_reasoning edges"],
  },
  {
    num: "06",
    key: "literature_retrieval",
    name: "Literature Retrieval",
    kind: "Tool",
    model: NO_LLM,
    desc: "Three independent live APIs — Europe PMC, PubMed E-utilities, Semantic Scholar — queried in parallel and merged by reciprocal rank fusion, so every citation traces back to a real query.",
    tags: ["3 live sources", "reciprocal rank fusion", "DOI dedup"],
  },
  {
    num: "07",
    key: "corrective_replanning",
    name: "Corrective Replanning",
    kind: "Agent",
    model: GEMINI,
    desc: "Event-driven off complications — selects from a bounded, pre-authored action library for that error's category, or escalates when nothing in the library fits. Never invents a novel procedure.",
    tags: ["bounded action library", "event-driven", "escalation path"],
  },
  {
    num: "08",
    key: "hitl_acknowledgment",
    name: "Proposal Acknowledgment",
    kind: "HITL",
    model: HUMAN,
    desc: "The surgeon acknowledges or dismisses a corrective proposal while the case is still live. Acknowledging keeps monitoring on as advisory; dismissing stops it entirely. The decision is durable graph state, so it survives a restart rather than living in a waiting process.",
    tags: ["acknowledge / dismiss", "durable graph state", "advisory downgrade"],
  },
  {
    num: "09",
    key: "divergence_detection",
    name: "Trajectory Divergence Detection",
    kind: "Agent",
    model: GEMINI,
    desc: "Polls only while a corrective proposal is actually live, and stops the moment it resolves. Deterministic graph-state check first — did the same error fire again — LLM reasoning only if that's ambiguous.",
    tags: ["deterministic first", "LLM fallback", "zero writes if followed"],
  },
  {
    num: "10",
    key: "alert_routing",
    name: "Alert Routing",
    kind: "Deterministic",
    model: NO_LLM,
    desc: "Writes the pending external action to the graph BEFORE it's sent, calls the verification gate synchronously, and only executes on a pass — so a blocked alert leaves a visible record of what didn't go out, and why.",
    tags: ["intent before write", "gate-checked", "real delivery outcome"],
  },
  {
    num: "11",
    key: "verification_gate",
    name: "Verification Gate",
    kind: "Deterministic",
    model: NO_LLM,
    desc: "Deliberately not an LLM — every check is a structural fact about the graph. Read-only by import, not by promise: it cannot write the external action it approves, only return pass or fail-closed.",
    tags: ["fail-closed", "read-only by design", "deterministic checks"],
  },
  {
    num: "12",
    key: "documentation",
    name: "Documentation",
    kind: "Agent",
    model: GEMINI,
    desc: "Drafts the operative record from the case's full reasoning graph. Screened autonomously by Model Armor the moment it's drafted, before a surgeon ever sees an Approve button.",
    tags: ["Model Armor screened", "HITL-gated"],
  },
  {
    num: "13",
    key: "hitl_record_approval",
    name: "Operative Record Approval",
    kind: "HITL",
    model: HUMAN,
    desc: "Approve, edit or reject the drafted record. An edit is re-screened by Model Armor and re-checked by the verification gate before anything is filed — human input is treated as untrusted, exactly like model output.",
    tags: ["approve / edit / reject", "re-screened on edit", "gates the FHIR write"],
  },
];

const SURGBOT_AGENTS: AgentCard[] = [
  {
    num: "14",
    key: "surgbot_root",
    name: "SurgBot Root",
    kind: "Agent",
    model: GEMINI,
    desc: "Leads the surgeon through a six-phase case review — loading the case, walking it phase by phase, opening up flagged errors and corrective proposals, then drafting the review for approval. Dispatches to specialist sub-agents as each phase needs them.",
    tags: ["6-phase review", "GEAP Agent Runtime", "voice-driven"],
  },
  {
    num: "15",
    key: "error_chain_reviewer",
    name: "Error Chain Reviewer",
    kind: "Agent",
    model: GEMINI,
    desc: "Explains the mechanism behind a flagged error and probes whether it holds up clinically, working from the same evidence chain the pipeline built.",
    tags: ["Agent Identity", "separately deployed", "phase 3"],
  },
  {
    num: "16",
    key: "synthesis",
    name: "Synthesis",
    kind: "Agent",
    model: GEMINI,
    desc: "Drafts the case review document from what the surgeon actually said during the session — agreements, disagreements, and coaching notes captured as they happened.",
    tags: ["Agent Identity", "separately deployed", "phase 5"],
  },
  {
    num: "17",
    key: "pattern_insight",
    name: "Pattern Insight",
    kind: "Agent",
    model: GEMINI,
    desc: "Surfaces patterns across a reviewer's previous sessions, drawing on the durable memory their earlier approvals wrote.",
    tags: ["Agent Identity", "Memory Bank", "phase 6"],
  },
  {
    num: "18",
    key: "feedback_router",
    name: "Feedback Router",
    kind: "Agent",
    model: GEMINI,
    desc: "Classifies free-text surgeon feedback as a standing directive or a case-grounded observation, and routes it to the pipeline agent it belongs to.",
    tags: ["Agent Identity", "directive vs observation", "learning loop"],
  },
  {
    num: "19",
    key: "hitl_review_approval",
    name: "Review Approval",
    kind: "HITL",
    model: HUMAN,
    desc: "Approving the case review is what turns captured feedback into durable knowledge. Nothing said during a session reaches the pipeline until the surgeon signs off on it.",
    tags: ["approval-gated learning", "approve / edit / reject", "writes to Memory Bank"],
  },
];

// Progressive disclosure: the collapsed card carries everything scannable —
// number, kind, name, model and the descriptive tags — and only the code
// identifier and the prose description wait behind hover or keyboard focus.
// The panel is absolutely positioned so an open card never reflows the grid
// around it, and `@media (hover: none)` in home.css keeps the full content
// permanently visible on touch, where hover does not exist.
function AgentGrid({ agents }: { agents: AgentCard[] }) {
  return (
    <div className="home__agent-grid">
      {agents.map((a) => (
        <div className="home__agent-card" key={a.key} tabIndex={0}>
          <div className="home__agent-card-top">
            <span className="home__agent-num">{a.num}</span>
            <span className={`home__agent-kind home__agent-kind--${a.kind.toLowerCase()}`}>{a.kind}</span>
          </div>
          <h4 className="home__agent-name">{a.name}</h4>
          {/* Dashed outline where no model runs — a non-colour way to show
              absence, so the distinction survives greyscale and colourblindness. */}
          <span
            className={`home__agent-model${a.model === NO_LLM || a.model === HUMAN ? " home__agent-model--none" : ""}`}
          >
            {a.model}
          </span>

          <div className="home__tag-row">
            {a.tags.map((t) => (
              <span className="home__tag" key={t}>{t}</span>
            ))}
          </div>

          <div className="home__agent-card-body">
            <div className="home__agent-card-body-inner">
              <div className="home__agent-key">{a.key}</div>
              <p className="home__agent-desc">{a.desc}</p>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export function AgentsSection() {
  return (
    <section className="home__section" id="agents">
      <span className="home__eyebrow">Orchestration</span>

      <div className="home__agent-root">
        <div className="home__agent-root-text">
          <span className="home__pill">SurgOS</span>
          <h2 className="home__agent-root-title">1 system. 2 workflows. 13 agents. 3 HITL gates.</h2>
          <p className="home__agent-root-desc">
            <b>SurgGraph</b> is a parallel agentic workflow deterministic flow by design — surgical analysis can afford
            probabilistic reasoning, but never a probabilistic execution path. 
          </p>
          <p className="home__agent-root-desc">
            <b>SurgBot</b> is the conversational feedback layer - 
            surgeon reviews what SurgGraph detected and their feedback becomes knowledge the next case inherits.
          </p>
        </div>
        <div className="home__orchestrator-flow-wrap">
          <OrchestratorFlow />
        </div>
      </div>

      <div className="home__agent-group">
        <span className="home__agent-group-label">SurgGraph — autonomous pipeline</span>
        <AgentGrid agents={SURGGRAPH_AGENTS} />
      </div>

      <div className="home__agent-group">
        <span className="home__agent-group-label">SurgBot — conversational review</span>
        <AgentGrid agents={SURGBOT_AGENTS} />
      </div>
    </section>
  );
}
