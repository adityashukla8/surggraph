import { NODE_TYPE_ICON, EDGE_KIND_COLOR, CONFIRMATION_STATUS_COLOR, EVENT_NODE_ACCENT } from "./palette";

// Static reference reflecting the real encoding in nodeTypes.tsx/edgeTypes.tsx/
// palette.ts — kept as literal entries (not derived from those maps) since
// the maps themselves don't carry human-readable descriptions, only the
// raw colors/icons the node/edge components consume.
const NODE_ROWS: { icon: string; color: string; label: string }[] = [
  { icon: NODE_TYPE_ICON.agent, color: "var(--text-secondary)", label: "Agent" },
  { icon: NODE_TYPE_ICON.phase, color: "var(--baseline)", label: "Phase / activity" },
  { icon: NODE_TYPE_ICON.entity, color: "var(--baseline)", label: "Entity (instrument / anatomy)" },
  { icon: NODE_TYPE_ICON.event, color: EVENT_NODE_ACCENT, label: "Event (divergence)" },
  { icon: NODE_TYPE_ICON.artifact, color: "var(--baseline)", label: "Artifact (real-world write)" },
];

const EDGE_ROWS: { swatch: React.CSSProperties; label: string }[] = [
  { swatch: { borderTop: `3px solid ${EDGE_KIND_COLOR.action}` }, label: "Action / dispatch" },
  { swatch: { borderTop: `3px solid ${EDGE_KIND_COLOR.observed}` }, label: "Observed (confirmed real)" },
  { swatch: { borderTop: "2px dashed var(--text-secondary)" }, label: "Predicted — pending" },
  { swatch: { borderTop: "2px dashed var(--text-muted)", opacity: 0.5 }, label: "Predicted — refuted" },
];

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
        <div className="graph-legend__section">
          <div className="graph-legend__heading">Nodes</div>
          {NODE_ROWS.map((row) => (
            <div className="graph-legend__row" key={row.label}>
              <span className="graph-legend__icon" style={{ color: row.color }}>
                {row.icon}
              </span>
              <span>{row.label}</span>
            </div>
          ))}
        </div>
        <div className="graph-legend__section">
          <div className="graph-legend__heading">Edges</div>
          {EDGE_ROWS.map((row) => (
            <div className="graph-legend__row" key={row.label}>
              <span className="graph-legend__swatch" style={row.swatch} />
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
