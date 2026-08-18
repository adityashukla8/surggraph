import { useMemo, useState } from "react";
import type { ReasoningLogEntry } from "../../graph/types";

interface EventInputPanelProps {
  log: ReasoningLogEntry[];
  onSubmit: (text: string) => Promise<void> | void;
}

// Matches the real naming convention in agents/error_detection/agent.py
// (error_detection_coordinator, _temporal, _spatial, _procedural)
// — a prefix check on the actual verified convention, not a guessed exact
// string (an earlier version hardcoded "monitor", which never matched any
// real source_agent value the Error Detection Agent actually emits).
const ERROR_DETECTION_AGENT_PREFIX = "error_detection";

// Rows carry the same tag format as the actions panel: wall clock, the case's
// own video time, then a pastel category tag, with the text underneath. The
// shared shape lives in one CSS rule for both panels (see App.css) so the two
// cannot drift apart; only the hues differ, and those continue the same
// palette rather than introducing new colours.
//
// The category IS the real source_agent — the three role sub-agents genuinely
// emit error_detection_{temporal,spatial,procedural} and the consensus step
// emits error_detection_aggregation. Nothing here is inferred from the text.
type Category = "temporal" | "spatial" | "procedural" | "aggregation" | "detection";

const CATEGORY_LABEL: Record<Category, string> = {
  temporal: "Temporal",
  spatial: "Spatial",
  procedural: "Procedural",
  aggregation: "Aggregation",
  detection: "Error Detected",
};

function categorize(entry: ReasoningLogEntry): Category {
  // An entry that wrote an actual error node is the verdict, not one role's
  // opinion — worth separating from the per-window commentary around it.
  if (entry.nodeType === "error") return "detection";
  if (entry.source_agent === "error_detection_temporal") return "temporal";
  if (entry.source_agent === "error_detection_spatial") return "spatial";
  if (entry.source_agent === "error_detection_procedural") return "procedural";
  return "aggregation";
}

/** The case's own clock. Wall time only says when the machine got round to it. */
function videoTime(seconds?: number): string {
  if (seconds === undefined) return "—:—";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function EventInputPanel({ log, onSubmit }: EventInputPanelProps) {
  const [draft, setDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const entries = useMemo(
    () =>
      log
        .filter((e) => e.source_agent.startsWith(ERROR_DETECTION_AGENT_PREFIX))
        .map((entry) => ({ entry, category: categorize(entry) })),
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
            <p className="tile__placeholder">No Error Detection activity yet.</p>
          ) : (
            <ul className="feed__list">
              {entries.map(({ entry, category }) => (
                <li key={entry.event_id} className="feed__item">
                  <span className="feed__row-tags">
                    <span className="feed__clock">{new Date(entry.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>
                    <span className="feed__vtime">{videoTime(entry.videoTimeS)}</span>
                    <span className={`feed__tag feed__tag--${category}`}>{CATEGORY_LABEL[category]}</span>
                  </span>
                  {/* Wraps rather than truncating. The actions panel can clip a
                      summary because the detail is one click away; here the
                      reason is the whole content and has nowhere else to go. */}
                  <span className="feed__reason">{entry.reason}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
