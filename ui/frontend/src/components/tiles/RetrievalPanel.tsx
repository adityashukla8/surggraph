import type { ReasoningLogEntry } from "../../graph/types";

interface RetrievalPanelProps {
  log: ReasoningLogEntry[];
}

const RETRIEVAL_AGENTS = new Set(["imaging", "literature", "complication_enumeration"]);

export function RetrievalPanel({ log }: RetrievalPanelProps) {
  const entries = log.filter((e) => RETRIEVAL_AGENTS.has(e.source_agent));

  return (
    <div className="tile" data-tile="retrieval">
      <div className="tile__header">
        <h3>Active Perception &amp; Retrieval</h3>
      </div>
      <div className="tile__body tile__body--column">
        {entries.length === 0 ? (
          <p className="tile__placeholder">
            No Imaging or Literature Agent activity yet — this fills in live once divergence triggers active MRI
            inspection and literature retrieval.
          </p>
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
