import type { EdgeKind, NodeType } from "./types";
import {
  NODE_TYPE_ICON,
  NODE_TYPE_ICON_IMAGE,
  NODE_TYPE_LABEL,
  EDGE_KIND_COLOR,
  CONFIRMATION_STATUS_COLOR,
  agentColorVar,
  nodeKindColorVar,
  nodeKindOutlineStyle,
} from "./palette";

// A factually accurate reference for the real encoding in nodeTypes.tsx /
// edgeTypes.tsx / palette.ts. Colors, icons, dash patterns and stroke widths
// are all READ FROM those same modules rather than restated as literals here —
// a legend that drifts from what the graph actually draws is worse than none,
// and hand-copied swatches are exactly how that drift happens. Only the
// human-readable descriptions and the grouping are authored here.

// The row's NAME is not authored here — it comes from NODE_TYPE_LABEL, the same
// map the nodes themselves tag with, so the legend and the graph can never call
// the same kind two different things. Only the explanatory note is authored.
type NodeRow = { kind: NodeType; note: string };

// Grouped by lifecycle stage — nineteen flat rows is a wall of text, and the
// grouping itself teaches the pipeline's shape.
const NODE_GROUPS: { heading: string; rows: NodeRow[] }[] = [
  {
    heading: "Perception",
    rows: [
      { kind: "entity", note: "instrument / anatomy / material" },
      { kind: "perception_event", note: "something changed" },
      { kind: "phase", note: "step in the procedure" },
      { kind: "snapshot", note: "what is true right now" },
      { kind: "vitals", note: "physiological state" },
    ],
  },
  {
    heading: "Reasoning",
    rows: [
      { kind: "error", note: "technique error — outline = severity" },
      { kind: "complication", note: "candidate complication" },
      { kind: "literature_evidence", note: "retrieved literature" },
      { kind: "corrective_trajectory", note: "what SHOULD happen next" },
      { kind: "divergence_alert", note: "divergence from the proposed path" },
    ],
  },
  {
    heading: "Action & safety",
    rows: [
      { kind: "action_intent", note: "proposed external write" },
      { kind: "verification_block", note: "fail-closed gate outcome" },
      { kind: "model_armor_screen", note: "content-safety screening outcome" },
      { kind: "action_outcome", note: "real-world write result" },
    ],
  },
  {
    heading: "Case",
    rows: [
      { kind: "trigger", note: "case opened — everything hangs off this" },
      { kind: "agent", note: "icon color = which agent" },
      { kind: "patient_twin", note: "patient profile (synthetic)" },
      { kind: "manual_event", note: "human-entered note" },
      { kind: "benchmark", note: "post-case scorecard" },
      { kind: "documentation", note: "operative note draft" },
    ],
  },
];

type EdgeRow = { kind: EdgeKind; label: string };

const EDGE_ROWS: EdgeRow[] = [
  { kind: "hierarchy", label: "Dispatch / hierarchy" },
  { kind: "involved", label: "Event involves entity" },
  { kind: "succession", label: "Followed in time" },
  { kind: "detection", label: "Detected from perception" },
  { kind: "causal_reasoning", label: "Error → possible complication" },
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
const STRUCTURAL: ReadonlySet<EdgeKind> = new Set<EdgeKind>(["hierarchy", "involved", "succession"]);
const EMPHASIZED: ReadonlySet<EdgeKind> = new Set<EdgeKind>(["confirmation", "verification"]);
const DASHED: ReadonlySet<EdgeKind> = new Set<EdgeKind>(["prediction", "proposal", "causal_reasoning"]);

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
                {/* Outline = node kind, icon = the kind's shape. Both read
                    straight from palette.ts, same as the real nodes — a
                    real uploaded icon here when one exists (masked with a
                    plain neutral tint, not an agent color: unlike a node on
                    the canvas, this row isn't any one real agent's output),
                    the same glyph fallback otherwise. */}
                <span
                  className="graph-legend__node-swatch"
                  style={{
                    borderColor: nodeKindColorVar(row.kind),
                    borderStyle: nodeKindOutlineStyle(row.kind),
                  }}
                >
                  {NODE_TYPE_ICON_IMAGE[row.kind] ? (
                    <span
                      className="graph-legend__node-icon-img"
                      style={{ maskImage: `url(${NODE_TYPE_ICON_IMAGE[row.kind]})`, WebkitMaskImage: `url(${NODE_TYPE_ICON_IMAGE[row.kind]})` }}
                    />
                  ) : (
                    NODE_TYPE_ICON[row.kind]
                  )}
                </span>
                <span>
                  {/* The same string the node's own tag shows, so a viewer can
                      match a tag on the canvas to a row here by eye. */}
                  <b className="graph-legend__name">{NODE_TYPE_LABEL[row.kind]}</b> — {row.note}
                </span>
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
