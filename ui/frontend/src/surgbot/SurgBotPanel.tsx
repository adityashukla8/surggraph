import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { PHASE_FALLBACK_LABELS, REVIEW_PHASES } from "./types";
import type { ReviewDocument, ReviewPhase, ToolCallEvent, TranscriptEntry } from "./types";
import { useSurgBotVoice } from "./useSurgBotVoice";
import { ReviewDocumentPanel } from "./ReviewDocumentPanel";
import "./surgbot.css";

// SurgBot — the voice-driven cross-case review panel. A docked right-hand
// column (App.tsx's resizable/collapsible split), not a floating overlay —
// earlier revisions used a fixed bottom-right blob + popup panel, superseded
// by a dedicated rail once the dashboard itself made room for one. Clicking
// the orb is the ONE control for both activating the panel's chat view and
// starting/stopping the voice session — there is no separate "open panel" vs
// "start talking" step.
//
// Case selection is deliberately NOT a form gating that click: the backend's
// own root_agent already exposes list_accessible_cases()/load_case_graph()
// as conversational tools (plan_v2 §14.1), so session_start intentionally
// sends an empty case_ids list — "which case(s)" is something the surgeon
// says out loud in Phase 1, not something this panel needs to collect first.

const REVIEWER_ID_KEY = "surgbot_reviewer_id";

function getOrCreateReviewerId(): string {
  try {
    const existing = window.localStorage.getItem(REVIEWER_ID_KEY);
    if (existing) return existing;
    const fresh = typeof crypto !== "undefined" && "randomUUID" in crypto
      ? `reviewer-${crypto.randomUUID().slice(0, 8)}`
      : `reviewer-${Date.now().toString(36)}`;
    window.localStorage.setItem(REVIEWER_ID_KEY, fresh);
    return fresh;
  } catch {
    // Private browsing / storage blocked — a per-tab id is still honest,
    // just not persisted across reloads.
    return `reviewer-${Date.now().toString(36)}`;
  }
}

/** Backend disclosure events carry the raw internal identifier (e.g.
 *  "surgbot_synthesis") — this is a presentation-only rename to the names
 *  used in the product; the wire protocol / tests still assert the raw
 *  strings, so only the frontend's display layer changes. Unmapped names
 *  (STT/TTS, or anything future) just render as-is, not silently dropped. */
const AGENT_DISPLAY_NAMES: Record<string, string> = {
  surgbot_root: "SurgBot Orchestrator",
  surgbot_error_chain_reviewer: "Error Chain Review Agent",
  surgbot_synthesis: "Case Review Agent",
  surgbot_pattern_insight: "Cross-cases Coaching Agent",
};

/** Both fields are required by the disclosure contract (plan_v2 §14 — "the
 *  UI cannot render half-complete"); a missing one renders as a literal,
 *  visible "unknown" rather than being silently dropped or guessed at. */
function agentDisplay(agentName: string | null): string {
  if (!agentName) return "unknown agent";
  return AGENT_DISPLAY_NAMES[agentName] ?? agentName;
}

function modelSurfaceDisplay(modelId: string | null, apiSurface: string | null): string {
  const model = modelId ?? "unknown model";
  const surface =
    apiSurface === "vertex_ai_global"
      ? "Vertex AI"
      : apiSurface === "google_cloud_speech"
        ? "Cloud Speech-to-Text"
        : apiSurface === "google_cloud_tts"
          ? "Cloud Text-to-Speech"
          : apiSurface === "vertex_ai_medasr"
            ? "Vertex AI (MedASR, self-deployed)"
            : "unknown API surface";
  return `${model} (${surface})`;
}

type FeedRow =
  | { kind: "transcript"; at: number; entry: TranscriptEntry }
  | { kind: "tool"; at: number; event: ToolCallEvent }
  | { kind: "document"; at: number; doc: ReviewDocument };

function buildFeed(
  transcript: TranscriptEntry[],
  toolEvents: ToolCallEvent[],
  reviewDocument: ReviewDocument | null,
): FeedRow[] {
  const rows: FeedRow[] = [
    ...transcript.map((entry): FeedRow => ({ kind: "transcript", at: entry.at, entry })),
    ...toolEvents.map((event): FeedRow => ({ kind: "tool", at: event.at, event })),
  ];
  // Rendered inline in the chronological feed (like a shared file bubble),
  // not pinned below it — real user report: a fixed 40vh block stuck to the
  // chat's bottom border made it impossible to keep chatting after the
  // document appeared.
  if (reviewDocument) rows.push({ kind: "document", at: reviewDocument.at, doc: reviewDocument });
  return rows.sort((a, b) => a.at - b.at);
}

function orbLabel(state: string): string {
  switch (state) {
    case "connecting":
      return "Connecting to SurgBot…";
    case "listening":
      return "Listening — release to send";
    case "speaking":
      return "SurgBot is speaking — press and hold to talk";
    case "connected-idle":
      return "Press and hold to talk";
    case "error":
      return "SurgBot connection lost — press to retry";
    default:
      return "Tap to Start Session";
  }
}

/** The living voice orb — layered rotating conic-gradients (technique
 *  adapted from a real reference implementation, SmoothUI's SiriOrb:
 *  github.com/educlopez/smoothui, packages/smoothui/components/siri-orb) so
 *  it reads as an organic, breathing sphere rather than a flat animated
 *  circle. State-driven purely via CSS classes (no per-frame JS/canvas) —
 *  `orbState` already reflects real connection/listening/speaking signals
 *  from useSurgBotVoice, so the orb's motion is never a fabricated cue. */
function Orb({ state, compact }: { state: string; compact?: boolean }) {
  return (
    <span className={`sb__orb sb__orb--${state}${compact ? " sb__orb--compact" : ""}`} aria-hidden="true">
      <span className="sb__orb-glow" />
      <span className="sb__orb-core" />
      <span className="sb__orb-sheen" />
      <span className="sb__orb-rim" />
    </span>
  );
}

interface SurgBotPanelProps {
  /** Width/flex-basis, controlled by App.tsx's resizable split state. */
  style?: CSSProperties;
  /** True when the user collapsed this side of the split to focus on
   *  SurgGraph — renders a slim reopen strip instead of the full panel. The
   *  underlying voice session (if any) is NOT torn down by collapsing: the
   *  useSurgBotVoice() hook stays mounted either way, so a live conversation
   *  keeps running in the background exactly as it would if left visible. */
  collapsed: boolean;
  onExpand: () => void;
}

export function SurgBotPanel({ style, collapsed, onExpand }: SurgBotPanelProps) {
  const [open, setOpen] = useState(false);
  const [showTextFallback, setShowTextFallback] = useState(false);
  const [fallbackText, setFallbackText] = useState("");
  // Tool/agent-use chips default to a collapsed (clamped) summary — clicking
  // one toggles it into this set to show its full, unclamped text.
  const [expandedChips, setExpandedChips] = useState<ReadonlySet<string>>(new Set());
  const reviewerIdRef = useRef<string>(getOrCreateReviewerId());
  const feedRef = useRef<HTMLDivElement | null>(null);

  function toggleChipExpanded(callId: string) {
    setExpandedChips((prev) => {
      const next = new Set(prev);
      if (next.has(callId)) next.delete(callId);
      else next.add(callId);
      return next;
    });
  }

  const {
    status,
    error,
    currentPhase,
    phaseLabel,
    transcript,
    toolEvents,
    reviewDocument,
    isListening,
    isModelSpeaking,
    start,
    stop,
    retry,
    startTalking,
    stopTalking,
    sendTextTurn,
    stopNarration,
  } = useSurgBotVoice();

  const orbState = useMemo(() => {
    if (status === "connecting") return "connecting";
    if (status === "disconnected") return "error";
    if (status === "connected" && isListening) return "listening";
    if (status === "connected" && isModelSpeaking) return "speaking";
    if (status === "connected") return "connected-idle";
    return "idle";
  }, [status, isListening, isModelSpeaking]);

  const feed = useMemo(
    () => buildFeed(transcript, toolEvents, reviewDocument),
    [transcript, toolEvents, reviewDocument],
  );

  useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [feed]);

  // Push-to-talk: pressing starts the session (first press) or a talking
  // turn (subsequent presses); releasing ends the turn — recording is sent
  // as one whole clip through the classic STT -> LLM -> TTS pipeline
  // (plan_v2 §15, services/surgbot_service/main.py). Ending the SESSION
  // entirely is a deliberately separate control (below) so it can never be
  // triggered by the same gesture as talking.
  function handleOrbPointerDown() {
    if (!open) {
      setOpen(true);
      // Case selection happens conversationally (see module docstring) — an
      // empty list is a deliberate, disclosed choice, not an omission.
      start([], reviewerIdRef.current);
      return;
    }
    if (status === "disconnected") {
      retry();
      return;
    }
    if (status === "connected") {
      startTalking();
    }
  }

  function handleOrbPointerUp() {
    if (open && status === "connected" && isListening) {
      stopTalking();
    }
  }

  function endSession() {
    setOpen(false);
    stop();
  }

  function submitFallbackText() {
    const text = fallbackText.trim();
    if (!text) return;
    sendTextTurn(text);
    setFallbackText("");
  }

  if (collapsed) {
    return (
      <button
        className="sb__collapsed-strip"
        style={style}
        onClick={onExpand}
        title="Expand SurgBot"
        aria-label="Expand SurgBot"
      >
        <span className={`sb__collapsed-dot sb__collapsed-dot--${orbState}`} aria-hidden="true" />
        <span className="sb__collapsed-label">SurgBot</span>
        <span className="sb__collapsed-chevron" aria-hidden="true">
          ‹
        </span>
      </button>
    );
  }

  return (
    <div className="sb__rail" style={style}>
      <header className="sb__header">
        <div className="sb__header-title">
          <h1>SurgBot</h1>
          <p className="sb__header-tagline">Cross-case QA Assistant for surgeons & safety leads</p>
        </div>
      </header>
      <div className="sb__content">
        {!open ? (
          <div className="sb__intro">
            <div className="sb__card">
              <h3 className="sb__card-title">Agents at work</h3>
              <ul className="sb__card-list sb__card-list--grid">
                <li>
                  <span className="sb__card-item-name">SurgBot Orchestrator</span>
                  <span className="sb__card-item-meta">Gemini 3.5 · tool dispatch &amp; reasoning</span>
                </li>
                <li>
                  <span className="sb__card-item-name">Error Chain Review Agent</span>
                  <span className="sb__card-item-meta">Gemini 3.5 · mechanism &amp; citations</span>
                </li>
                <li>
                  <span className="sb__card-item-name">Case Review Agent</span>
                  <span className="sb__card-item-meta">Gemini 3.5 · drafts the case review</span>
                </li>
                <li>
                  <span className="sb__card-item-name">Cross-cases Coaching Agent</span>
                  <span className="sb__card-item-meta">Gemini 3.5 · cross-session coaching</span>
                </li>
                <li>
                  <span className="sb__card-item-name">Speech-to-Text</span>
                  <span className="sb__card-item-meta">MedASR (self-deployed) · transcription</span>
                </li>
                <li>
                  <span className="sb__card-item-name">Text-to-Speech</span>
                  <span className="sb__card-item-meta">Cloud TTS (Chirp 3 HD) · voice synthesis</span>
                </li>
              </ul>
            </div>

            <div className="sb__card">
              <h3 className="sb__card-title">GEAP services in use</h3>
              <ul className="sb__card-list sb__card-list--grid">
                <li>
                  <span className="sb__card-item-name">Agent Runtime</span>
                  <span className="sb__card-item-meta">hosts all 4 agents above</span>
                </li>
                <li>
                  <span className="sb__card-item-name">Agent Registry</span>
                  <span className="sb__card-item-meta">makes agents discoverable</span>
                </li>
                <li>
                  <span className="sb__card-item-name">Agent Identity</span>
                  <span className="sb__card-item-meta">SPIFFE identity on the Gemini 3.5 subagents</span>
                </li>
                <li>
                  <span className="sb__card-item-name">Memory Bank</span>
                  <span className="sb__card-item-meta">remembers your review patterns across sessions</span>
                </li>
                <li>
                  <span className="sb__card-item-name">Model Armor</span>
                  <span className="sb__card-item-meta">screens drafts before they're approvable</span>
                </li>
              </ul>
            </div>

            <div className="sb__card">
              <h3 className="sb__card-title">Ask things like…</h3>
              <ul className="sb__card-examples">
                <li>&ldquo;List the cases available for review&rdquo;</li>
                <li>&ldquo;Walk me through phase 2 of this case&rdquo;</li>
                <li>&ldquo;What caused the error in phase 3?&rdquo;</li>
                <li>&ldquo;Was that corrective proposal justified?&rdquo;</li>
                <li>&ldquo;Draft the review document&rdquo;</li>
                <li>&ldquo;Have I flagged this kind of issue before?&rdquo;</li>
              </ul>
            </div>
          </div>
        ) : (
          <div className="sb__chat" role="dialog" aria-label="SurgBot voice review">
            <div className="sb__status-row">
              <span className={`sb__status-dot sb__status-dot--${status}`} aria-hidden="true" />
              <span className="sb__status-text">
                {status === "idle" && "Not connected"}
                {status === "connecting" && "Connecting…"}
                {status === "connected" && "Connected"}
                {status === "disconnected" && (error ?? "Connection lost")}
              </span>
              {/* Real user report: no way to stop a long narration without
                  ending the whole session. Only shown while SurgBot is
                  actually speaking — stopping the current turn is a
                  different action from ending the session (below), so
                  each gets its own control rather than overloading one. */}
              {isModelSpeaking && (
                <button className="sb__stop-narration-btn" onClick={stopNarration} title="Stop narration">
                  Stop narration
                </button>
              )}
              {/* Ending the session is deliberately its own control, separate
                  from the orb — push-to-talk now owns the orb's press/hold
                  gesture entirely, so ending the session can never be
                  triggered by the same gesture as talking. */}
              <button className="sb__end-session-btn" onClick={endSession} title="End session">
                End session
              </button>
            </div>

            <ol className="sb__stepper" aria-label="Review phase">
              {REVIEW_PHASES.map((phase) => {
                const isCurrent = currentPhase === phase;
                const isPast = currentPhase !== null && phase < currentPhase;
                const label = isCurrent && phaseLabel ? phaseLabel : PHASE_FALLBACK_LABELS[phase as ReviewPhase];
                return (
                  <li
                    key={phase}
                    className={`sb__step${isCurrent ? " sb__step--current" : ""}${isPast ? " sb__step--past" : ""}`}
                    title={label}
                  >
                    <span className="sb__step-num">{phase}</span>
                    <span className="sb__step-label">{label}</span>
                  </li>
                );
              })}
            </ol>

            <div className="sb__feed" ref={feedRef}>
              {feed.length === 0 ? (
                <p className="sb__feed-empty">
                  {status === "connected" ? "Say hello to start the review." : "Waiting to connect…"}
                </p>
              ) : (
                feed.map((row) => {
                  if (row.kind === "transcript") {
                    return (
                      <div key={row.entry.id} className={`sb__turn sb__turn--${row.entry.speaker}`}>
                        {row.entry.text}
                        {!row.entry.final && <span className="sb__turn-cursor" aria-hidden="true">▌</span>}
                      </div>
                    );
                  }
                  if (row.kind === "document") {
                    return <ReviewDocumentPanel key={row.doc.review_id} reviewDocument={row.doc} />;
                  }
                  const isExpanded = expandedChips.has(row.event.call_id);
                  return (
                    <button
                      key={row.event.call_id}
                      type="button"
                      className={`sb__tool-chip sb__tool-chip--${row.event.status}${isExpanded ? " sb__tool-chip--expanded" : ""}`}
                      onClick={() => toggleChipExpanded(row.event.call_id)}
                      aria-expanded={isExpanded}
                    >
                      <span className="sb__tool-chip-agent">{agentDisplay(row.event.agent_name)}</span>
                      <span className="sb__tool-chip-sep">·</span>
                      <span className="sb__tool-chip-model">{modelSurfaceDisplay(row.event.model_id, row.event.api_surface)}</span>
                      <span className="sb__tool-chip-tool">{row.event.tool_name}</span>
                      {row.event.status === "in_progress" ? (
                        <span className="sb__tool-chip-state">running…</span>
                      ) : (
                        row.event.summary && <span className="sb__tool-chip-summary">{row.event.summary}</span>
                      )}
                    </button>
                  );
                })
              )}
            </div>

            {status === "disconnected" && (
              <div className="sb__reconnect-prompt" role="alert">
                <span className="sb__reconnect-text">
                  {error ?? "Connection lost"} — your conversation so far is kept.
                </span>
                <button className="sb__btn sb__btn--primary" onClick={retry}>
                  Tap to continue
                </button>
              </div>
            )}

            <div className="sb__fallback">
              <button className="sb__fallback-toggle" onClick={() => setShowTextFallback((v) => !v)}>
                {showTextFallback ? "Hide text fallback" : "Having trouble? Type instead"}
              </button>
              {showTextFallback && (
                <div className="sb__fallback-row">
                  <input
                    className="sb__fallback-input"
                    type="text"
                    value={fallbackText}
                    placeholder="Type a turn…"
                    onChange={(e) => setFallbackText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") submitFallbackText();
                    }}
                  />
                  <button className="sb__btn" onClick={submitFallbackText} disabled={status !== "connected"}>
                    Send
                  </button>
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      <div className={`sb__orb-dock${open ? " sb__orb-dock--compact" : ""}`}>
        <button
          className="sb__orb-btn"
          onPointerDown={handleOrbPointerDown}
          onPointerUp={handleOrbPointerUp}
          onPointerLeave={handleOrbPointerUp}
          onPointerCancel={handleOrbPointerUp}
          aria-pressed={isListening}
          aria-label={orbLabel(orbState)}
          title={orbLabel(orbState)}
        >
          <Orb state={orbState} compact={open} />
        </button>
        <span className="sb__orb-caption">{orbLabel(orbState)}</span>
      </div>
    </div>
  );
}
