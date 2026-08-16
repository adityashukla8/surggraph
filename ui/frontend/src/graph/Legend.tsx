import type { EdgeKind, NodeType } from "./types";
import { NODE_TYPE_ICON, EDGE_KIND_COLOR, CONFIRMATION_STATUS_COLOR, agentColorVar, nodeKindColorVar, nodeKindOutlineStyle } from "./palette";

// A factually accurate reference for the real encoding in nodeTypes.tsx /
// edgeTypes.tsx / palette.ts. Colors, icons, dash patterns and stroke widths
// are all READ FROM those same modules rather than restated as literals here —
// a legend that drifts from what the graph actually draws is worse than none,
// and hand-copied swatches are exactly how that drift happens. Only the
// human-readable descriptions and the grouping are authored here.

type NodeRow = { kind: NodeType; label: string };

// Grouped by lifecycle stage — eighteen flat rows is a wall of text, and the
// grouping itself teaches the pipeline's shape.
const NODE_GROUPS: { heading: string; rows: NodeRow[] }[] = [
  {
    heading: "Perception",
    rows: [
      { kind: "entity", label: "Entity — instrument / anatomy / material" },
      { kind: "perception_event", label: "Perception event — something changed" },
      { kind: "phase", label: "Phase / step" },
      { kind: "snapshot", label: "Snapshot — what is true right now" },
      { kind: "vitals", label: "Physiological state" },
    ],
  },
  {
    heading: "Reasoning",
    rows: [
      { kind: "error", label: "Technique error (OCHRA-grounded)" },
      { kind: "complication", label: "Complication candidate" },
      { kind: "literature_evidence", label: "Literature evidence" },
      { kind: "corrective_trajectory", label: "Corrective proposal — what SHOULD happen" },
      { kind: "divergence_alert", label: "Divergence from the proposed path" },
    ],
  },
  {
    heading: "Action & safety",
    rows: [
      { kind: "action_intent", label: "Proposed external write" },
      { kind: "verification_block", label: "Fail-closed gate outcome" },
      { kind: "action_outcome", label: "Real-world write result" },
    ],
  },
  {
    heading: "Case",
    rows: [
      { kind: "agent", label: "Agent — icon color = which agent" },
      { kind: "patient_twin", label: "Patient profile (synthetic)" },
      { kind: "manual_event", label: "Human-entered note" },
      { kind: "benchmark", label: "Post-case scorecard" },
      { kind: "documentation", label: "Operative note draft" },
    ],
  },
];

type EdgeRow = { kind: EdgeKind; label: string };

const EDGE_ROWS: EdgeRow[] = [
  { kind: "hierarchy", label: "Dispatch / hierarchy" },
  { kind: "involved", label: "Event involves entity" },
  { kind: "detection", label: "Detected from perception" },
  { kind: "causal_reasoning", label: "Error → complication" },
  { kind: "evidence", label: "Grounded in literature" },
  { kind: "prediction", label: "Predicted (color = predicting agent)" },
  { kind: "proposal", label: "Proposed corrective action" },
  { kind: "trajectory_comparison", label: "Actual vs. proposed" },
  { kind: "confirmation", label: "Prediction confirmed by reality" },
  { kind: "verification", label: "Verification outcome" },
  { kind: "outcome", label: "Write → delivery result" },
  { kind: "grading", label: "Graded vs. ground truth (post-case)" },
];

// Mirrors edgeTypes.tsx::edgeStyle exactly, so a change to how edges really
// render shows up here too rather than leaving the legend quietly wrong.
const STRUCTURAL: ReadonlySet<EdgeKind> = new Set<EdgeKind>(["hierarchy", "involved"]);
const EMPHASIZED: ReadonlySet<EdgeKind> = new Set<EdgeKind>(["confirmation", "verification"]);
const DASHED: ReadonlySet<EdgeKind> = new Set<EdgeKind>(["prediction", "proposal"]);

function edgeSwatch(kind: EdgeKind): React.CSSProperties {
  const width = STRUCTURAL.has(kind) ? 1 : EMPHASIZED.has(kind) ? 3 : 1.5;
  // Prediction edges really are per-agent colored, so the swatch shows one
  // concrete agent's color rather than a hue no real edge ever uses.
  const color = kind === "prediction" ? agentColorVar("anticipation") : EDGE_KIND_COLOR[kind];
  return {
    borderTop: `${width}px ${DASHED.has(kind) ? "dashed" : "solid"} ${color}`,
    ...(STRUCTURAL.has(kind) ? { opacity: 0.55 } : {}),
  };
}

const STATUS_ROWS: { color: string; label: string }[] = [
  { color: CONFIRMATION_STATUS_COLOR.pending, label: "Pending" },
  { color: CONFIRMATION_STATUS_COLOR.confirmed, label: "Confirmed" },
  { color: CONFIRMATION_STATUS_COLOR.refuted, label: "Refuted" },
];

export function GraphLegend() {
  return (
    // Bare `open` (not `open={true}`/`open={false}`) — an uncontrolled
    // native attribute, so it starts expanded (a viewer shouldn't have to
    // click to understand the graph) but the browser still owns the toggle
    // after that: clicking <summary> to collapse it isn't fought by React
    // on the next re-render, unlike a React-controlled boolean prop would.
    <details className="graph-legend" open>
      <summary className="graph-legend__toggle">Legend</summary>
      <div className="graph-legend__body">
        {NODE_GROUPS.map((group) => (
          <div className="graph-legend__section" key={group.heading}>
            <div className="graph-legend__heading">{group.heading}</div>
            {group.rows.map((row) => (
              <div className="graph-legend__row" key={row.kind}>
                {/* Outline = node kind, icon glyph = the kind's shape. Both
                    read straight from palette.ts, same as the real nodes. */}
                <span
                  className="graph-legend__node-swatch"
                  style={{
                    borderColor: nodeKindColorVar(row.kind),
                    borderStyle: nodeKindOutlineStyle(row.kind),
                  }}
                >
                  {NODE_TYPE_ICON[row.kind]}
                </span>
                <span>{row.label}</span>
              </div>
            ))}
          </div>
        ))}
        <div className="graph-legend__section">
          <div className="graph-legend__heading">Edges</div>
          {EDGE_ROWS.map((row) => (
            <div className="graph-legend__row" key={row.kind}>
              <span className="graph-legend__swatch" style={edgeSwatch(row.kind)} />
              <span>{row.label}</span>
            </div>
          ))}
        </div>
        <div className="graph-legend__section">
          <div className="graph-legend__heading">Confirmation status</div>
          {STATUS_ROWS.map((row) => (
            <div className="graph-legend__row" key={row.label}>
              <span className="graph-legend__dot" style={{ background: row.color }} />
              <span>{row.label}</span>
            </div>
          ))}
        </div>
      </div>
    </details>
  );
}
