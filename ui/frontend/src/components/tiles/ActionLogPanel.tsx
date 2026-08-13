import type { ReasoningLogEntry } from "../../graph/types";

interface ActionLogPanelProps {
  log: ReasoningLogEntry[];
}

// UNVERIFIED: agents/action_router, agents/verifier don't exist yet, so
// these are guessed names, not confirmed against real source_agent values
// (the same mistake EventInputPanel.tsx made for Monitor before it was
// built and this could be checked — see its fix). Re-verify/fix this
// filter once those agents are real.
const ROUTING_AGENTS = new Set(["action_router", "verifier"]);

export function ActionLogPanel({ log }: ActionLogPanelProps) {
  const entries = log.filter((e) => ROUTING_AGENTS.has(e.source_agent));

  return (
    <div className="tile" data-tile="action-log">
      <div className="tile__header">
        <h3>Autonomous Action Log</h3>
      </div>
      <div className="tile__body tile__body--column">
        {entries.length === 0 ? (
          <p className="tile__placeholder">No routing decisions yet — Verifier checks and Action Router decisions appear here.</p>
        ) : (
          <ul className="event-input__list">
            {entries.map((entry) => (
              <li key={entry.event_id}>
                <span className="event-input__time">{new Date(entry.timestamp).toLocaleTimeString()}</span>
                <span className="retrieval__agent">{entry.source_agent}</span>
                <span className="event-input__reason">{entry.reason}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
