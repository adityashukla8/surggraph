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

/** Which real service produced a tool/agent-use event (plan_v2 §15: classic
 *  STT -> LLM -> TTS pipeline). Every reasoning step (root agent and every
 *  subagent) runs on real Gemini 3.5 over the plain Vertex AI surface; the
 *  two speech stages disclose their own real service. All three are
 *  disclosed identically and none is ever hidden — see the disclosure
 *  requirement in SurgBotPanel.tsx. */
export type ApiSurface = "vertex_ai_global" | "google_cloud_speech" | "google_cloud_tts";

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

/** Push-to-talk turn boundaries (plan_v2 §16 — classic STT -> LLM -> TTS
 *  pipeline, streamed both directions). mic_start opens a real server-side
 *  StreamingRecognize session; the client then streams binary PCM frames
 *  to it AS CAPTURED (not accumulated); mic_stop finalizes that
 *  already-in-progress recognition — see services/surgbot_service/main.py. */
export interface MicStartMessage {
  type: "mic_start";
}

export interface MicStopMessage {
  type: "mic_stop";
}

/** Stops the current turn's narration immediately (plan_v2 §17 — real user
 *  report: no way to stop a long response without ending the whole
 *  session). The conversation itself is untouched — the reviewer can
 *  start a new turn right away. */
export interface StopNarrationMessage {
  type: "stop_narration";
}

export type ClientMessage =
  | SessionStartMessage
  | TextTurnMessage
  | EndSessionMessage
  | MicStartMessage
  | StopNarrationMessage
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

export interface ServerErrorMessage {
  type: "error";
  detail: string;
}

export type ServerMessage =
  | PhaseChangedMessage
  | ToolCallStartedMessage
  | ToolCallFinishedMessage
  | TranscriptDeltaMessage
  | ReviewDocumentReadyMessage
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
  /** Date.now() when this review_id first appeared — kept stable across
   *  later status updates (approve/edit/reject) so its position in the
   *  chronological feed doesn't jump every time its status changes. */
  at: number;
}
