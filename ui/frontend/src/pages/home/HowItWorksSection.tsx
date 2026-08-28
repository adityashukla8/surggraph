import { PipelineFlow } from "./PipelineFlow";

interface StepCard {
  num: string;
  color: string;
  label: string;
  title: string;
  desc: string;
  tags: string[];
}

const STEPS: StepCard[] = [
  {
    num: "01",
    color: "var(--home-accent)",
    label: "Perceive",
    title: "Perception + Error Detection",
    desc: "Sweeps the video in windows. Live video → surgeon activity, entities, relations, activities, OCHRA-graded errors.",
    tags: ["Multimodal", "5s windows", "3-agent consensus", "Weighted Aggregation", "Graph-driven State"],
  },
  {
    num: "02",
    color: "var(--home-accent-dark)",
    label: "Reason",
    title: "Complication Reasoning + Literature + Replanning",
    desc: "Detected error → reasoned complication → literature-backed → bounded corrective proposal.",
    tags: ["Trajectory Analysis", "Literature-grounded Complications", "Replanning Agent"],
  },
  {
    num: "03",
    color: "var(--home-orange)",
    label: "Act",
    title: "Divergence Detection + Alert Routing + Documentation",
    desc: "Watches for actual-vs-proposed divergence, routes verified alerts, drafts the operative record.",
    tags: ["Human-in-the-Loop-gated"],
  },
  {
    num: "04",
    color: "var(--home-green)",
    label: "Verify",
    title: "Verification Gate + Model Armor",
    desc: "Every external write passes both a structural evidence gate and a content-safety layer before it leaves the system.",
    tags: ["Model Armor", "Human-in-the-Loop", "Fail-closed Design"],
  },
];

export function HowItWorksSection() {
  return (
    <section className="home__section" id="how-it-works">
      <span className="home__eyebrow">SurgGraph</span>
      <h2 className="home__headline" style={{ fontSize: 38, margin: "0 0 8px" }}>
        The Autonomous Workflow
      </h2>
      {/* <p className="home__hiw-layer-label">Core TaskMaster Track Submission</p> */}
      <p className="home__hiw-subheading">Core TaskMaster Track Submission</p>

      <div className="home__pipeline-card">
        <div>
          <span className="home__pill">Pipeline</span>
          <h3 className="home__pipeline-title">Autonomous Workflow</h3>
          <ul className="home__pipeline-desc-list">
            <li>Patient pre-op data + Real-time Vitals + Surgical video continuously consumed by Gemini 3.5</li>
            <li>Autonomously evolving state graph serves as real-time context layer for agents</li>
            <li>Autonomous delegation across 9 agents</li>
            <li>Autonomous alerts on dashboard</li>
            <li>Human in the loop design</li>
          </ul>
        </div>
        <PipelineFlow />
      </div>

      <div className="home__step-grid">
        {STEPS.map((s) => (
          <div
            className="home__step-card"
            key={s.num}
            style={{ ["--step-color" as string]: s.color, scrollMarginTop: 90 }}
          >
            <span className="home__step-num">{s.num} · {s.label}</span>
            <div className="home__step-bars" aria-hidden="true">
              {Array.from({ length: 9 }).map((_, i) => (
                <span
                  key={i}
                  className="home__step-bar"
                  style={{ height: `${18 + ((i * 37 + s.num.charCodeAt(0)) % 28)}px` }}
                />
              ))}
            </div>
            <h4 className="home__step-title">{s.title}</h4>
            <p className="home__step-desc">{s.desc}</p>
            <div className="home__tag-row">
              {s.tags.map((t) => (
                <span className="home__tag" key={t}>{t}</span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
