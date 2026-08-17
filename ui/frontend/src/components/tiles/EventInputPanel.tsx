import { useMemo, useState } from "react";
import type { GraphNodePatch, ReasoningLogEntry } from "../../graph/types";
import { focusNode } from "../../graph/useGraphFocus";

// Manual Event Input / Error Detection Feed.
//
// Two halves. The input records a human-typed case note on the graph, tagged
// source_agent="human" and never dressed up as an agent's inference. The feed
// is the detector's own running commentary: every graph write carries a
// human-readable reason, and for Error Detection that is the three role
// sub-agents' actual reasoning, window by window — including the windows that
// never fired, which the graph alone never shows.
//
// Each detection is shown with the activity it happened during, resolved from
// the phase -> error link the detector now writes. An error with no surrounding
// context reads as a machine complaining about nothing in particular.

interface Props {
  log: ReasoningLogEntry[];
  nodes: GraphNodePatch[];
  edges: { source: string; target: string; edgeKind: string }[];
  onSubmit: (text: string) => Promise<void> | void;
}

// Category comes from the real source_agent, which the sub-agents genuinely
// emit as error_detection_{temporal,spatial,procedural}. Colours are pastel and
// match the actions panel's tagging so the two read as one system.
type Category = "temporal" | "spatial" | "procedural" | "aggregation" | "detection" | "human";

const CATEGORY_LABEL: Record<Category, string> = {
  temporal: "Temporal",
  spatial: "Spatial",
  procedural: "Procedural",
  aggregation: "Aggregation",
  detection: "Error Detected",
  human: "Case Note",
};

function categorize(entry: ReasoningLogEntry): Category | null {
  if (entry.source_agent === "human") return "human";
  // An entry that wrote an actual error node is the verdict, not a role's
  // opinion — worth distinguishing from the three per-window commentaries.
  if (entry.nodeType === "error") return "detection";
  if (entry.source_agent === "error_detection_temporal") return "temporal";
  if (entry.source_agent === "error_detection_spatial") return "spatial";
  if (entry.source_agent === "error_detection_procedural") return "procedural";
  if (entry.source_agent === "error_detection_aggregation") return "aggregation";
  if (entry.source_agent.startsWith("error_detection")) return "aggregation";
  return null; // everything else belongs to the other panels
}

function videoTime(seconds?: number): string {
  if (seconds === undefined) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function EventInputPanel({ log, nodes, edges, onSubmit }: Props) {
  const [draft, setDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // error node id -> the activity it was detected during, from the real
  // phase -> error detection edges rather than by matching timestamps here.
  const activityByError = useMemo(() => {
    const byId = new Map(nodes.map((n) => [n.node_id, n]));
    const map = new Map<string, GraphNodePatch>();
    for (const e of edges) {
      if (e.edgeKind !== "detection") continue;
      const src = byId.get(e.source);
      const tgt = byId.get(e.target);
      if (src?.node_type === "phase" && tgt?.node_type === "error") map.set(e.target, src);
    }
    return map;
  }, [nodes, edges]);

  const entries = useMemo(
    () => log.map((e) => ({ entry: e, category: categorize(e) })).filter((r): r is { entry: ReasoningLogEntry; category: Category } => r.category !== null),
    [log],
  );

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!draft.trim() || submitting) return;
    setSubmitting(true);
    try {
      await onSubmit(draft.trim());
      setDraft("");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="tile" data-tile="event-input">
      <div className="tile__header">
        <h3>Manual Event Input / Error Detection Feed</h3>
      </div>
      <div className="tile__body tile__body--column">
        <form className="event-input__form" onSubmit={handleSubmit}>
          <input
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder='e.g. "blood loss 400 ml", "giving tranexamic acid"'
            disabled={submitting}
          />
          <button type="submit" disabled={submitting || !draft.trim()}>
            Inject
          </button>
        </form>

        <div className="event-input__feed">
          {entries.length === 0 ? (
            <p className="tile__placeholder">
              No detector activity yet. Each window's temporal, spatial and procedural reasoning appears here as it happens.
            </p>
          ) : (
            <ul className="feed__list">
              {entries.map(({ entry, category }) => {
                const activity = entry.nodeId ? activityByError.get(entry.nodeId) : undefined;
                return (
                  <li key={entry.event_id} className="feed__row">
                    <div className="feed__meta">
                      <span className="feed__clock">{new Date(entry.timestamp).toLocaleTimeString()}</span>
                      <span className="feed__vtime">{videoTime(entry.videoTimeS)}</span>
                      <span className={`feed__tag feed__tag--${category}`}>{CATEGORY_LABEL[category]}</span>
                    </div>
                    <div className="feed__reason">{entry.reason}</div>
                    {activity && (
                      <div className="feed__during">
                        during{" "}
                        <button className="feed__link" onClick={() => focusNode(activity.node_id)} title="Show this activity in the graph">
                          {activity.label}
                        </button>
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
