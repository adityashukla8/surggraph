import "./App.css";
import { VideoPanel } from "./components/tiles/VideoPanel";
import { AnnotatedVideoPanel } from "./components/tiles/AnnotatedVideoPanel";
import { StateGraphPanel } from "./components/tiles/StateGraphPanel";
import { EventInputPanel } from "./components/tiles/EventInputPanel";
import { RetrievalPanel } from "./components/tiles/RetrievalPanel";
import { ActionLogPanel } from "./components/tiles/ActionLogPanel";
import { useCaseStateStream } from "./graph/useCaseStateStream";
import { useSyncedVideos } from "./video/useSyncedVideos";

const STATE_SERVICE_URL = import.meta.env.VITE_STATE_SERVICE_URL ?? "http://localhost:8080";

// TODO: replace with the case_id selected via the real case-open flow
// (GCS upload -> Eventarc trigger -> Orchestrator opens the case) once
// that's wired up (see plan §5/§6, Day 2).
const CASE_ID = import.meta.env.VITE_DEMO_CASE_ID ?? "demo-case-001";
// TODO: same as above — video_id should come from the opened case, not an
// env var, once Orchestrator exists.
const VIDEO_ID = import.meta.env.VITE_DEMO_VIDEO_ID ?? "video_01";
const RAW_VIDEO_URL = `${STATE_SERVICE_URL}/media/video/${VIDEO_ID}/video_left.mp4`;
const ANNOTATED_VIDEO_URL = `${STATE_SERVICE_URL}/media/video/${VIDEO_ID}/video_left_annotated.mp4`;

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
  const [rawVideoRef, annotatedVideoRef] = useSyncedVideos();

  return (
    <div className="dashboard">
      <header className="dashboard__header">
        <h1>SurgGraph</h1>
        <span className={`dashboard__status dashboard__status--${status}`}>state service: {status}</span>
      </header>
      <main className="dashboard__grid">
        <VideoPanel videoUrl={RAW_VIDEO_URL} videoRef={rawVideoRef} />
        <AnnotatedVideoPanel videoUrl={ANNOTATED_VIDEO_URL} videoRef={annotatedVideoRef} />
        <StateGraphPanel nodes={nodes} edges={edges} status={status} error={error} />
        <EventInputPanel log={log} onSubmit={(text) => submitManualEvent(CASE_ID, text)} />
        <RetrievalPanel log={log} />
        <ActionLogPanel log={log} />
      </main>
    </div>
  );
}

export default App;
