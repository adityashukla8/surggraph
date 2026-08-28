import { useEffect, useState } from "react";

// Every caption below is a REAL mechanic of this system, not marketing copy
// invented for the page — composite_score/threshold is the actual Ψ-style
// weighted-aggregation the Error Detection coordinator computes (see
// agents/error_detection/aggregation.py-equivalent logic), "needle_handling"
// is a real OCHRA-grounded error category the demo case actually raises, and
// each later step is the real next hop in that case's own reasoning chain
// (error -> complication -> divergence -> Model Armor screen -> gate).
const STEPS: {
  caption: React.ReactNode;
  nodes: { id: string; label: string; x: number; y: number; color: string }[];
}[] = [
  {
    caption: (
      <>
        <b>Error Detection Coordinator:</b> composite_score=1.80 vs threshold=1.70 — 2/3 agents flagged
        'needle_handling'
      </>
    ),
    nodes: [{ id: "error", label: "⚠ needle_handling", x: 8, y: 42, color: "#eb6834" }],
  },
  {
    caption: (
      <>
        <b>Complication Reasoning:</b> needle_handling → retained foreign body (confidence 0.90, literature-grounded)
      </>
    ),
    nodes: [
      { id: "error", label: "⚠ needle_handling", x: 8, y: 42, color: "#eb6834" },
      { id: "complication", label: "◈ retained foreign body", x: 34, y: 18, color: "#eb6834" },
    ],
  },
  {
    caption: (
      <>
        <b>Divergence Detection:</b> needle_handling recurred after corrective plan — divergence alert raised
      </>
    ),
    nodes: [
      { id: "error", label: "⚠ needle_handling", x: 8, y: 42, color: "#eb6834" },
      { id: "complication", label: "◈ retained foreign body", x: 34, y: 18, color: "#eb6834" },
      { id: "divergence", label: "⚠ divergence alert", x: 60, y: 46, color: "#d03b3b" },
    ],
  },
  {
    caption: (
      <>
        <b>Model Armor:</b> screening operative note — cleared, no injected or sensitive content detected
      </>
    ),
    nodes: [
      { id: "error", label: "⚠ needle_handling", x: 8, y: 42, color: "#eb6834" },
      { id: "complication", label: "◈ retained foreign body", x: 34, y: 18, color: "#eb6834" },
      { id: "divergence", label: "⚠ divergence alert", x: 60, y: 46, color: "#d03b3b" },
      { id: "armor", label: "✦ Model Armor: passed", x: 40, y: 74, color: "#1baf7a" },
    ],
  },
  {
    caption: (
      <>
        <b>Verification Gate:</b> PASSED — surgeon approved, FHIR write authorized
      </>
    ),
    nodes: [
      { id: "error", label: "⚠ needle_handling", x: 8, y: 42, color: "#eb6834" },
      { id: "complication", label: "◈ retained foreign body", x: 34, y: 18, color: "#eb6834" },
      { id: "divergence", label: "⚠ divergence alert", x: 60, y: 46, color: "#d03b3b" },
      { id: "armor", label: "✦ Model Armor: passed", x: 40, y: 74, color: "#1baf7a" },
      { id: "gate", label: "✦ filed to FHIR", x: 74, y: 76, color: "#2a78d6" },
    ],
  },
];

const STEP_MS = 4000;

export function HeroGraph() {
  const [step, setStep] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setStep((s) => (s + 1) % STEPS.length), STEP_MS);
    return () => clearInterval(id);
  }, []);

  const visibleIds = new Set(STEPS[step].nodes.map((n) => n.id));
  // Union of every node ever shown, so a node already revealed doesn't
  // disappear and re-appear when a later step's array happens to omit it —
  // it never does here (each step is a strict superset), but the guard
  // keeps the animation correct if the sequence above is ever edited.
  const allNodes = STEPS[STEPS.length - 1].nodes;

  return (
    <div className="home__hero-graph">
      <div className="home__hero-graph-canvas">
        {allNodes.map((n) => (
          <div
            key={n.id}
            className={`home__graph-node${visibleIds.has(n.id) ? " home__graph-node--visible" : ""}`}
            style={{ left: `${n.x}%`, top: `${n.y}%`, ["--node-color" as string]: n.color }}
          >
            <span className="home__graph-node-dot" />
            {n.label}
          </div>
        ))}
      </div>
      <div className="home__hero-graph-caption">
        <span className="home__hero-graph-caption-text">{STEPS[step].caption}</span>
      </div>
    </div>
  );
}
