import { useState } from "react";
import type { ReasoningLogEntry } from "../../graph/types";

interface EventInputPanelProps {
  log: ReasoningLogEntry[];
  onSubmit: (text: string) => Promise<void> | void;
}

// Matches the real naming convention in agents/monitor/agent.py
// (monitor_coordinator, monitor_temporal, monitor_spatial, monitor_procedural)
// — a prefix check on the actual verified convention, not a guessed exact
// string (an earlier version hardcoded "monitor", which never matched any
// real source_agent value the Monitor Agent actually emits).
const MONITOR_AGENT_PREFIX = "monitor";

export function EventInputPanel({ log, onSubmit }: EventInputPanelProps) {
  const [draft, setDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const monitorEvents = log.filter((e) => e.source_agent.startsWith(MONITOR_AGENT_PREFIX));

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
        <h3>Manual Event Input / Monitor Feed</h3>
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
          {monitorEvents.length === 0 ? (
            <p className="tile__placeholder">No Monitor Agent activity yet.</p>
          ) : (
            <ul className="event-input__list">
              {monitorEvents.map((entry) => (
                <li key={entry.event_id}>
                  <span className="event-input__time">{new Date(entry.timestamp).toLocaleTimeString()}</span>
                  <span className="event-input__reason">{entry.reason}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
