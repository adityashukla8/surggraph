import { FlowLegend } from "./flowTheme";
import { PipelineFlow } from "./PipelineFlow";
import { SurgBotPipelineFlow } from "./SurgBotPipelineFlow";

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
  {
    num: "05",
    color: "var(--home-violet)",
    label: "Review",
    title: "SurgBot + Feedback Loop",
    desc: "The surgeon reviews the filed case by voice. Approved feedback becomes durable knowledge that four SurgGraph agents read on the next case.",
    tags: ["Voice Review", "4 Subagents on Agent Runtime", "Memory Bank", "Approval-gated", "Advisory-only"],
  },
];

export function HowItWorksSection() {
  return (
    <section className="home__section" id="how-it-works">
      <span className="home__eyebrow">How it Works</span>
      <h2 className="home__headline" style={{ fontSize: 38, margin: "0 0 20px" }}>
        Workflow
      </h2>

      {/* Two workflows, two canvases, one shared legend. Separate canvases so
          each fitView zooms to its own content — sharing one would scale both
          to the wider diagram and shrink SurgBot's labels for no reason. */}
      <div className="home__pipeline-card">
        <FlowLegend />
        <div className="home__flow-split">
          <PipelineFlow />
          <SurgBotPipelineFlow />
        </div>
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
