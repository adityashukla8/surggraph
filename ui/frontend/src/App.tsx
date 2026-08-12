import "./App.css";
import { VideoPanel } from "./components/tiles/VideoPanel";
import { AnnotatedVideoPanel } from "./components/tiles/AnnotatedVideoPanel";
import { StateGraphPanel } from "./components/tiles/StateGraphPanel";
import { EventInputPanel } from "./components/tiles/EventInputPanel";
import { RetrievalPanel } from "./components/tiles/RetrievalPanel";
import { ActionLogPanel } from "./components/tiles/ActionLogPanel";
import { useCaseStateStream } from "./graph/useCaseStateStream";

const STATE_SERVICE_URL = import.meta.env.VITE_STATE_SERVICE_URL ?? "http://localhost:8080";

// TODO: replace with the case_id selected via the real case-open flow
// (GCS upload -> Eventarc trigger -> Orchestrator opens the case) once
// that's wired up (see plan §5/§6, Day 2).
const CASE_ID = import.meta.env.VITE_DEMO_CASE_ID ?? "demo-case-001";

async function submitManualEvent(caseId: string, text: string): Promise<void> {
  const resp = await fetch(`${STATE_SERVICE_URL}/events/manual`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ case_id: caseId, text }),
  });
  if (!resp.ok) {
    throw new Error(`manual event injection failed: ${resp.status}`);
  }
}

function App() {
  const { nodes, edges, log, status, error } = useCaseStateStream(CASE_ID);

  return (
    <div className="dashboard">
      <header className="dashboard__header">
        <h1>SurgGraph</h1>
        <span className={`dashboard__status dashboard__status--${status}`}>state service: {status}</span>
      </header>
      <main className="dashboard__grid">
        <VideoPanel />
        <AnnotatedVideoPanel />
        <StateGraphPanel nodes={nodes} edges={edges} status={status} error={error} />
        <EventInputPanel log={log} onSubmit={(text) => submitManualEvent(CASE_ID, text)} />
        <RetrievalPanel log={log} />
        <ActionLogPanel log={log} />
      </main>
    </div>
  );
}

export default App;
