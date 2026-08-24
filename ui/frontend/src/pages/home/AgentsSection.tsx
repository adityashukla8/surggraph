import { OrchestratorFlow } from "./OrchestratorFlow";

interface AgentCard {
  num: string;
  color: string;
  key: string;
  name: string;
  desc: string;
  tags: string[];
}

const AGENTS: AgentCard[] = [
  {
    num: "01",
    color: "var(--home-aqua)",
    key: "perception_agent",
    name: "Perception",
    desc: "One Gemini call per ~5s window over the case video, feeding a deterministic change-diff layer that decides what the graph actually hears about — entities, relations, activity progression.",
    tags: ["5s windows", "change-diff + debounce", "entity registry"],
  },
  {
    num: "02",
    color: "var(--home-orange)",
    key: "error_detection",
    name: "Error Detection",
    desc: "Three independent sub-agents (temporal, spatial, procedural) reason over the same window from different angles; a coordinator weighs their votes against OCHRA-grounded error categories.",
    tags: ["3-agent consensus", "OCHRA-grounded", "weighted aggregation"],
  },
  {
    num: "03",
    color: "var(--home-red)",
    key: "complication_reasoning",
    name: "Complication Reasoning",
    desc: "Event-driven off error nodes above a severity threshold — formulates a literature query from live case context, reasons over the retrieved abstracts, and writes complications linked back to their evidence.",
    tags: ["event-driven", "literature-grounded", "causal_reasoning edges"],
  },
  {
    num: "04",
    color: "var(--home-accent)",
    key: "literature_retrieval",
    name: "Literature Retrieval",
    desc: "A tool-wrapper agent over three independent live APIs — Europe PMC, PubMed E-utilities, Semantic Scholar — merged by reciprocal rank fusion so every citation traces back to a real query.",
    tags: ["3 live sources", "reciprocal rank fusion", "DOI dedup"],
  },
  {
    num: "05",
    color: "var(--home-magenta)",
    key: "corrective_replanning",
    name: "Corrective Replanning",
    desc: "Event-driven off complications — selects from a bounded, pre-authored action library for that error's category, or escalates when nothing in the library fits. Never invents a novel procedure.",
    tags: ["bounded action library", "event-driven", "escalation path"],
  },
  {
    num: "06",
    color: "var(--home-violet)",
    key: "divergence_detection",
    name: "Trajectory Divergence Detection",
    desc: "Polls only while a corrective proposal is actually live, and stops the moment it resolves. Deterministic graph-state check first — did the same error fire again — LLM reasoning only if that's ambiguous.",
    tags: ["deterministic first", "LLM fallback", "zero writes if followed"],
  },
  {
    num: "07",
    color: "var(--home-yellow)",
    key: "alert_routing",
    name: "Alert Routing",
    desc: "Writes the pending external action to the graph BEFORE it's sent, calls the verification gate synchronously, and only executes on a pass — so a blocked alert leaves a visible record of what didn't go out, and why.",
    tags: ["intent before write", "gate-checked", "real delivery outcome"],
  },
  {
    num: "08",
    color: "var(--home-aqua)",
    key: "documentation",
    name: "Documentation",
    desc: "Drafts the operative record as a byproduct of reasoning that already happened on the graph — screened autonomously by Model Armor the moment it's drafted, before a surgeon ever sees an Approve button.",
    tags: ["byproduct of reasoning", "Model Armor screened", "HITL-gated"],
  },
  {
    num: "09",
    color: "var(--home-green)",
    key: "verification_gate",
    name: "Verification Gate",
    desc: "Deliberately not an LLM — every check is a structural fact about the graph. Read-only by import, not by promise: it cannot write the external action it approves, only return pass or fail-closed.",
    tags: ["fail-closed", "read-only by design", "deterministic checks"],
  },
];

export function AgentsSection() {
  return (
    <section className="home__section" id="agents">
      <span className="home__eyebrow">Agents</span>
      <h2 className="home__headline" style={{ fontSize: 38, maxWidth: 700 }}>
        One orchestrator. Nine specialists.
      </h2>
      <p className="home__lede" style={{ marginBottom: 40 }}>
        SurgGraph routes every real signal — a perceived activity, a detected error, a resolved divergence — to the
        specialist that owns it, rather than one model trying to reason about the whole case at once.
      </p>

      <div className="home__agent-root">
        <div className="home__agent-root-text">
          <span className="home__pill">Root Agent</span>
          <h3 className="home__agent-root-title">Orchestrator</h3>
          <p className="home__agent-root-desc">
            Opens the case and dispatches Perception Agent and Error Detection Workflow
            concurrently over the video. Registers every event-driven specialist before the sweep starts, so a
            reasoning trigger fired the moment a sweep begins is never missed.
          </p>
          <div className="home__tag-row">
            <span className="home__tag">Parallel delegation</span>
            <span className="home__tag">9 specialist agents</span>
            <span className="home__tag">Event bus, not polling</span>
          </div>
        </div>
        <div className="home__orchestrator-flow-wrap">
          <OrchestratorFlow />
        </div>
      </div>

      <div className="home__agent-grid">
        {AGENTS.map((a) => (
          <div className="home__agent-card" key={a.key} style={{ ["--agent-color" as string]: a.color }}>
            <span className="home__agent-num">{a.num}</span>
            <div className="home__agent-key">{a.key}</div>
            <h4 className="home__agent-name">{a.name}</h4>
            <p className="home__agent-desc">{a.desc}</p>
            <div className="home__tag-row">
              {a.tags.map((t) => (
                <span className="home__tag" key={t}>{t}</span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
