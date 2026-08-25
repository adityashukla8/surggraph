// Mirrors agents/surgbot/schema.py's SurgBot protocol vocabulary (ReviewPhase,
// ToolUseDisclosure, CaseReviewDocument) and the exact WebSocket wire shapes
// fixed by plan_v2 §14.4 (the backend-agent protocol this client is built
// against). Kept in sync by hand — the Python side is authoritative once it
// exists; see docs/plan_v2/consider-the-initial-11082026-md-harmonic-cloud.md
// §14. This is a new, independent file: nothing here is imported from
// graph/types.ts, since none of it is genuinely shared with the existing
// case-graph stream.

/** Renumbered to a clean 6-phase script, no gap — mirrors agents/surgbot/
 *  root_agent.py's _INSTRUCTION, the source of truth for phase numbering.
 *  (Earlier revision had a 7-phase script with 5 skipped — superseded.) */
export type ReviewPhase = 1 | 2 | 3 | 4 | 5 | 6;

export const REVIEW_PHASES: readonly ReviewPhase[] = [1, 2, 3, 4, 5, 6];

/** Fallback labels only — the backend's own `phase_label` on `phase_changed`
 *  is always preferred where present (see useSurgBotVoice.ts). These exist so
 *  the stepper has something honest to show for a phase the session hasn't
 *  reached (and therefore has no server-provided label for) yet. */
export const PHASE_FALLBACK_LABELS: Record<ReviewPhase, string> = {
  1: "Case framing",
  2: "Chronological graph walkthrough",
  3: "Error-and-complication review",
  4: "Corrective proposals & divergences",
  5: "Synthesis & case review document",
  6: "Cross-session pattern review",
};

/** Which real API surface produced a tool/agent-use event. Gemini Live API
 *  drives voice turn-taking only; every actual reasoning step is a Gemini 3.5
 *  subagent call over the plain Vertex AI (non-Live) surface. Both values are
 *  disclosed identically and neither is ever hidden — see the disclosure
 *  requirement in SurgBotPanel.tsx. */
export type ApiSurface = "vertex_ai_global" | "vertex_ai_live";

export type ApprovalStatus = "drafting" | "pending" | "blocked" | "approved" | "rejected" | "edited";

export type ApprovalOutcome = "approved" | "rejected" | "edited";

// ---------------------------------------------------------------------------
// Client -> server
// ---------------------------------------------------------------------------

export interface SessionStartMessage {
  type: "session_start";
  case_ids: string[];
  reviewer_id: string;
}

/** Fallback/manual path only — not the primary UX (voice is). */
export interface TextTurnMessage {
  type: "text_turn";
  text: string;
}

export interface EndSessionMessage {
  type: "end_session";
}

/** Push-to-talk turn boundaries — real user report + real Cloud Logging
 *  evidence: automatic VAD-triggered barge-in reliably risked crashing the
 *  deployed agent's own internal event queue. automatic_activity_detection
 *  is now disabled server-side (services/surgbot_service/main.py); these
 *  mark exactly when a real user turn begins/ends instead, forwarded as
 *  LiveRequest.activity_start/activity_end. */
export interface MicStartMessage {
  type: "mic_start";
}

export interface MicStopMessage {
  type: "mic_stop";
}

export type ClientMessage =
  | SessionStartMessage
  | TextTurnMessage
  | EndSessionMessage
  | MicStartMessage
  | MicStopMessage;

// ---------------------------------------------------------------------------
// Server -> client
// ---------------------------------------------------------------------------

export interface PhaseChangedMessage {
  type: "phase_changed";
  phase: ReviewPhase;
  phase_label: string;
}

/** agent_name/model_id/api_surface are the disclosure-required fields. They
 *  are typed optional here on purpose, even though the protocol calls them
 *  required: the whole point of the disclosure requirement is that a chip
 *  must render a visible "unknown" rather than silently drop when the
 *  backend sends an incomplete event, so the frontend must handle the
 *  "missing" case rather than assume it away with a non-optional type. */
export interface ToolCallStartedMessage {
  type: "tool_call_started";
  call_id: string;
  agent_name?: string | null;
  model_id?: string | null;
  api_surface?: ApiSurface | string | null;
  tool_name: string;
  args_summary?: string;
}

export interface ToolCallFinishedMessage {
  type: "tool_call_finished";
  call_id: string;
  summary?: string;
}

export interface TranscriptDeltaMessage {
  type: "transcript_delta";
  speaker: "user" | "model";
  text: string;
  final: boolean;
}

export interface ReviewDocumentReadyMessage {
  type: "review_document_ready";
  review_id: string;
  sections: Record<string, string>;
  approval_status: ApprovalStatus;
}

export interface SessionResumptionHandleMessage {
  type: "session_resumption_handle";
  handle: string;
}

export interface ServerErrorMessage {
  type: "error";
  detail: string;
}

/** Real barge-in signal: ADK's Event.interrupted, forwarded verbatim
 *  (services/surgbot_service/main.py) whenever server-side voice-activity
 *  detection cancels the model's in-progress generation because the reviewer
 *  started talking over it. No payload beyond the type — the only correct
 *  client action is to immediately stop whatever PCM audio is already
 *  queued/playing (see useSurgBotVoice.ts), not to wait for more data. */
export interface InterruptedMessage {
  type: "interrupted";
}

export type ServerMessage =
  | PhaseChangedMessage
  | ToolCallStartedMessage
  | ToolCallFinishedMessage
  | TranscriptDeltaMessage
  | ReviewDocumentReadyMessage
  | SessionResumptionHandleMessage
  | InterruptedMessage
  | ServerErrorMessage;

// ---------------------------------------------------------------------------
// Derived UI-facing state (not on the wire — built by useSurgBotVoice.ts)
// ---------------------------------------------------------------------------

/** A single tool/agent-use disclosure row. `agent_name`/`model_id`/
 *  `api_surface` are `null` (never dropped, never defaulted to a guess) when
 *  the backend event omitted them — the UI renders a visible "unknown" for
 *  each missing field individually. */
export interface ToolCallEvent {
  call_id: string;
  agent_name: string | null;
  model_id: string | null;
  api_surface: ApiSurface | string | null;
  tool_name: string;
  args_summary?: string;
  summary?: string;
  status: "in_progress" | "finished";
  /** Date.now() at first sight — local ordering only, never sent anywhere. */
  at: number;
}

export interface TranscriptEntry {
  id: string;
  speaker: "user" | "model";
  text: string;
  final: boolean;
  at: number;
}

export interface ReviewDocument {
  review_id: string;
  sections: Record<string, string>;
  approval_status: ApprovalStatus;
}
