import { useRef, useState } from "react";
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
const ORCHESTRATOR_URL = import.meta.env.VITE_ORCHESTRATOR_URL ?? "http://localhost:8090";

// TODO: video_id should come from a real case-selection flow once more than
// one video exists — today there's exactly one demo video, referenced by id.
const VIDEO_ID = import.meta.env.VITE_DEMO_VIDEO_ID ?? "video_01";
const RAW_VIDEO_URL = `${STATE_SERVICE_URL}/media/video/${VIDEO_ID}/video_left.mp4`;
const ANNOTATED_VIDEO_URL = `${STATE_SERVICE_URL}/media/video/${VIDEO_ID}/video_left_annotated.mp4`;

async function openCase(videoId: string): Promise<string> {
  const resp = await fetch(`${ORCHESTRATOR_URL}/cases/open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video_id: videoId }),
  });
  if (!resp.ok) {
    throw new Error(`case open failed: ${resp.status}`);
  }
  const body = (await resp.json()) as { case_id: string };
  return body.case_id;
}

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
  // null until the user presses play — page load alone is not the trigger.
  // The autonomous pipeline starts on that first play (services/
  // orchestrator_service's POST /cases/open), not on mount, so a case_id
  // minted for one viewer's session is never implied by merely loading the
  // page — see agents/orchestrator/agent.py's module docstring for why this
  // is the real trigger, and why every trigger mints a fully isolated case.
  const [caseId, setCaseId] = useState<string | null>(null);
  const triggering = useRef(false);

  const { nodes, edges, log, status, error } = useCaseStateStream(caseId);
  const [rawVideoRef, annotatedVideoRef] = useSyncedVideos();

  function handleFirstPlay() {
    if (triggering.current || caseId !== null) return; // only the first play triggers a case
    triggering.current = true;
    openCase(VIDEO_ID)
      .then(setCaseId)
      .catch((err) => {
        triggering.current = false; // allow retrying on the next play if this failed
        console.error("failed to open case:", err);
      });
  }

  return (
    <div className="dashboard">
      <header className="dashboard__header">
        <h1>SurgGraph</h1>
        <span className={`dashboard__status dashboard__status--${status}`}>
          {status === "idle" ? "press play to begin" : `state service: ${status}`}
        </span>
      </header>
      <main className="dashboard__grid">
        <VideoPanel videoUrl={RAW_VIDEO_URL} videoRef={rawVideoRef} onPlay={handleFirstPlay} />
        <AnnotatedVideoPanel videoUrl={ANNOTATED_VIDEO_URL} videoRef={annotatedVideoRef} />
        <StateGraphPanel nodes={nodes} edges={edges} status={status} error={error} />
        <EventInputPanel log={log} onSubmit={(text) => (caseId ? submitManualEvent(caseId, text) : Promise.resolve())} />
        <RetrievalPanel log={log} />
        <ActionLogPanel log={log} />
      </main>
    </div>
  );
}

export default App;
