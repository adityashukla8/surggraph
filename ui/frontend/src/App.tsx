import { useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import "./App.css";
import { VideoPanel } from "./components/tiles/VideoPanel";
import { StateGraphPanel } from "./components/tiles/StateGraphPanel";
import { EventInputPanel } from "./components/tiles/EventInputPanel";
import { CaseContextPanel } from "./components/tiles/CaseContextPanel";
import { AutonomousActionsPanel } from "./components/tiles/AutonomousActionsPanel";
import { setActiveCaseId } from "./api/hitl";
import { useCaseStateStream } from "./graph/useCaseStateStream";
import { SurgBotPanel } from "./surgbot/SurgBotPanel";

const STATE_SERVICE_URL = import.meta.env.VITE_STATE_SERVICE_URL ?? "http://localhost:8080";
const ORCHESTRATOR_URL = import.meta.env.VITE_ORCHESTRATOR_URL ?? "http://localhost:8090";

// TODO: video_id should come from a real case-selection flow once more than
// one video exists — today there's exactly one demo video, referenced by id.
// SurgGraph / SurgBot split — resizable by dragging the divider, collapsible
// from either side. Bounds keep both panels usably wide even at the drag
// extremes; a fully "collapsed" side isn't reachable by dragging at all —
// only the divider's own collapse buttons snap to that, rendering a slim
// reopen strip instead of squished, unreadable content.
const DEFAULT_SPLIT_PCT = 80;
const MIN_SPLIT_PCT = 50;
const MAX_SPLIT_PCT = 92;
const COLLAPSED_STRIP_PX = 40;

const VIDEO_ID = import.meta.env.VITE_DEMO_VIDEO_ID ?? "video_01";
const RAW_VIDEO_URL = `${STATE_SERVICE_URL}/media/video/${VIDEO_ID}/video_left.mp4`;
// The annotated/overlay tile was removed from the UI (caused seek/sync
// confusion for no benefit — its ground-truth overlay data isn't consumed
// by any other feature). The generation pipeline (scripts/prepare_demo_videos.py),
// the GCS-served file itself, and the sync hook (video/useSyncedVideos.ts)
// are deliberately kept, unused, rather than deleted — see that hook's
// module docstring for the real bugs it took to get dual-video sync right,
// in case a future feature needs it again.

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

function App() {
  // null until the user presses play — page load alone is not the trigger.
  // The autonomous pipeline starts on that first play (services/
  // orchestrator_service's POST /cases/open), not on mount, so a case_id
  // minted for one viewer's session is never implied by merely loading the
  // page — see agents/orchestrator/agent.py's module docstring for why this
  // is the real trigger, and why every trigger mints a fully isolated case.
  const [caseId, setCaseId] = useState<string | null>(null);
  const triggering = useRef(false);

  const [splitPct, setSplitPct] = useState(DEFAULT_SPLIT_PCT);
  const [surgGraphCollapsed, setSurgGraphCollapsed] = useState(false);
  const [surgBotCollapsed, setSurgBotCollapsed] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const draggingRef = useRef(false);

  function handleDividerPointerDown(e: ReactPointerEvent<HTMLDivElement>) {
    draggingRef.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function handleDividerPointerMove(e: ReactPointerEvent<HTMLDivElement>) {
    if (!draggingRef.current || !containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    const pct = ((e.clientX - rect.left) / rect.width) * 100;
    setSplitPct(Math.min(MAX_SPLIT_PCT, Math.max(MIN_SPLIT_PCT, pct)));
  }
  function handleDividerPointerUp(e: ReactPointerEvent<HTMLDivElement>) {
    draggingRef.current = false;
    e.currentTarget.releasePointerCapture(e.pointerId);
  }

  const mainStyle = surgGraphCollapsed
    ? { flex: `0 0 ${COLLAPSED_STRIP_PX}px`, maxWidth: `${COLLAPSED_STRIP_PX}px` }
    : surgBotCollapsed
      ? { flex: "1 1 auto", maxWidth: `calc(100% - ${COLLAPSED_STRIP_PX}px)` }
      : { flex: `0 0 ${splitPct}%`, maxWidth: `${splitPct}%` };

  const botStyle = surgBotCollapsed
    ? { flex: `0 0 ${COLLAPSED_STRIP_PX}px`, maxWidth: `${COLLAPSED_STRIP_PX}px` }
    : surgGraphCollapsed
      ? { flex: "1 1 auto", maxWidth: `calc(100% - ${COLLAPSED_STRIP_PX}px)` }
      : { flex: `0 0 ${100 - splitPct}%`, maxWidth: `${100 - splitPct}%` };

  // nodes/edges are what the canvas draws (collapsed branches removed);
  // allNodes/allEdges are the complete graph the side panels reason over.
  const { nodes, edges, allNodes, allEdges, log, status, error } = useCaseStateStream(caseId);

  function handleFirstPlay() {
    if (triggering.current || caseId !== null) return; // only the first play triggers a case
    triggering.current = true;
    openCase(VIDEO_ID)
      .then((id) => {
        setCaseId(id);
        // Node-level HITL controls act on whatever case the graph is showing,
        // so they read the id from here rather than holding their own copy.
        setActiveCaseId(id);
      })
      .catch((err) => {
        triggering.current = false; // allow retrying on the next play if this failed
        console.error("failed to open case:", err);
      });
  }

  const showDivider = !surgGraphCollapsed && !surgBotCollapsed;

  return (
    <div className="dashboard" ref={containerRef}>
      <div className="dashboard__main" style={mainStyle}>
        {surgGraphCollapsed ? (
          <button
            className="dashboard__collapsed-strip"
            onClick={() => setSurgGraphCollapsed(false)}
            title="Expand SurgGraph"
            aria-label="Expand SurgGraph"
          >
            <span className="dashboard__collapsed-chevron" aria-hidden="true">
              ›
            </span>
            <span className="dashboard__collapsed-label">SurgGraph</span>
          </button>
        ) : (
          <>
            <header className="dashboard__header">
              <div className="dashboard__title-block">
                <div className="dashboard__title-row">
                  <h1>SurgGraph</h1>
                  <span className="dashboard__track-label">TaskMaster Track</span>
                </div>
                <p className="dashboard__tagline">
                  Autonomous Workflow to Detect errors, predict complications, corrective measures & divergences, &
                  write operative reports
                </p>
              </div>
              <span className={`dashboard__status dashboard__status--${status}`}>
                {status === "idle" ? "Play the video to begin" : `state service: ${status}`}
              </span>
            </header>
            <main className="dashboard__grid">
              <VideoPanel videoUrl={RAW_VIDEO_URL} onPlay={handleFirstPlay} />
              <StateGraphPanel nodes={nodes} edges={edges} status={status} error={error} />
              <EventInputPanel log={log} />
              <CaseContextPanel caseId={caseId} nodes={allNodes} />
              <AutonomousActionsPanel
                caseId={caseId}
                nodes={allNodes}
                edges={allEdges.map((e) => ({ source: e.source, target: e.target, edgeKind: e.data?.edgeKind ?? "" }))}
              />
            </main>
          </>
        )}
      </div>

      {showDivider && (
        <div
          className="dashboard__divider"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize SurgGraph / SurgBot split — drag to resize"
          onPointerDown={handleDividerPointerDown}
          onPointerMove={handleDividerPointerMove}
          onPointerUp={handleDividerPointerUp}
        >
          <button
            className="dashboard__divider-btn dashboard__divider-btn--left"
            onPointerDown={(e) => e.stopPropagation()}
            onClick={() => setSurgGraphCollapsed(true)}
            title="Collapse SurgGraph"
            aria-label="Collapse SurgGraph"
          >
            ‹
          </button>
          <div className="dashboard__divider-grip" aria-hidden="true" />
          <button
            className="dashboard__divider-btn dashboard__divider-btn--right"
            onPointerDown={(e) => e.stopPropagation()}
            onClick={() => setSurgBotCollapsed(true)}
            title="Collapse SurgBot"
            aria-label="Collapse SurgBot"
          >
            ›
          </button>
        </div>
      )}

      <SurgBotPanel style={botStyle} collapsed={surgBotCollapsed} onExpand={() => setSurgBotCollapsed(false)} />
    </div>
  );
}

export default App;
