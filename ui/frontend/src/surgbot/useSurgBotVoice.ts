import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ApiSurface,
  ApprovalStatus,
  ClientMessage,
  ReviewDocument,
  ReviewPhase,
  ServerMessage,
  ToolCallEvent,
  TranscriptDeltaMessage,
  TranscriptEntry,
} from "./types";
import { MIC_WORKLET_NAME, PCM_OUTPUT_SAMPLE_RATE, PcmChunkPlayer, getMicWorkletBlobUrl } from "./audioWorklet";

const SURGBOT_SERVICE_URL = import.meta.env.VITE_SURGBOT_SERVICE_URL ?? "http://127.0.0.1:8091";

export type ConnectionStatus = "idle" | "connecting" | "connected" | "disconnected";

function wsUrlFor(sessionId: string): string {
  const httpUrl = new URL(SURGBOT_SERVICE_URL);
  httpUrl.protocol = httpUrl.protocol === "https:" ? "wss:" : "ws:";
  httpUrl.pathname = `${httpUrl.pathname.replace(/\/$/, "")}/surgbot/${sessionId}/voice`;
  return httpUrl.toString();
}

function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `sess-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

interface SurgBotVoiceResult {
  status: ConnectionStatus;
  error: string | null;
  currentPhase: ReviewPhase | null;
  phaseLabel: string | null;
  transcript: TranscriptEntry[];
  toolEvents: ToolCallEvent[];
  reviewDocument: ReviewDocument | null;
  /** True only while the user is actively holding the push-to-talk control
   *  (mic audio is being captured AND the server has been told a real turn
   *  is in progress) — not merely "mic capture is attached." */
  isListening: boolean;
  /** True for the WHOLE classic STT -> LLM -> TTS pipeline of one turn
   *  (plan_v2 §15) — from the moment the held clip is sent (mic_stop) or a
   *  text_turn is sent, until the synthesized reply clip finishes playing
   *  (or the turn errors out). Broader than "audio is literally playing":
   *  covers real transcription + reasoning/tool-call time too, so the orb
   *  reads as "SurgBot is working" for the entire round trip, not just its
   *  last leg. */
  isModelSpeaking: boolean;
  start: (caseIds: string[], reviewerId: string) => void;
  stop: () => void;
  /** Push-to-talk: startTalking() on press sends mic_start and begins
   *  forwarding each captured chunk to the server AS IT ARRIVES — real
   *  StreamingRecognize on the server side (plan_v2 §16), not a
   *  send-one-blob-on-release design, so most of the turn's transcription
   *  work overlaps the reviewer's own hold duration. stopTalking() on
   *  release sends mic_stop, finalizing that already-in-progress
   *  recognition. Mic audio is only ever sent between the two calls. */
  startTalking: () => void;
  stopTalking: () => void;
  /** Reconnects after a disconnect WITHOUT wiping transcript/toolEvents/
   *  reviewDocument — unlike start(), which is a genuinely fresh session.
   *  Real user report ("when the bot crashes midway, instead of wiping
   *  everything and reconnecting, we need safe fallbacks that continue the
   *  assistant and prompt to try again"): reconnection used to be silent and
   *  automatic (scheduleRetry), which either felt invisible or, if the user
   *  got impatient and clicked the orb mid-retry, actually called stop()
   *  (since `open` was still true) followed by a fresh start() that wiped
   *  history. Now a disconnect is never auto-retried — it surfaces a visible
   *  prompt (SurgBotPanel.tsx) that calls this instead. */
  retry: () => void;
  /** The fallback/manual path (§14 protocol's `text_turn`) — not the primary
   *  UX, exposed only for SurgBotPanel's small escape-hatch toggle. Typed
   *  input gets a typed reply back (speak=False server-side, plan_v2
   *  §17) — no narration, so both modalities stay genuinely usable on
   *  their own terms rather than one always dragging the other along. */
  sendTextTurn: (text: string) => void;
  /** Stops the current turn's narration immediately (plan_v2 §17 — real
   *  user report: no way to stop a long response without ending the whole
   *  session). Stops local playback right away — doesn't wait for the
   *  server's stop_narration acknowledgment — and lets the conversation
   *  continue normally afterward. */
  stopNarration: () => void;
}

/** Opens the SurgBot voice WebSocket, captures the mic as 16kHz mono PCM16
 *  and forwards each chunk to the server as it's captured while held (real
 *  server-side StreamingRecognize, plan_v2 §16 — classic STT -> LLM -> TTS,
 *  not a bidi stream, but not a send-one-blob-on-release design either),
 *  plays back the reply's PCM16 chunks as they stream in — one roughly per
 *  sentence, scheduled back-to-back via PcmChunkPlayer — and parses
 *  control-channel JSON frames into React state.
 *
 * Status contract deliberately mirrors useCaseStateStream.ts's shape
 * ({status, error}) even though the transport here is a WebSocket, not SSE:
 * "idle" until start() is called, "connecting" while the socket + mic are
 * being set up, "connected" once the session_start handshake is sent,
 * "disconnected" (with a populated `error`) on any drop. Unlike the graph
 * stream, this does NOT auto-retry silently — a real user report ("crashes
 * midway, wiping everything") found that a silent background retry loop
 * either felt invisible or, if the user clicked the orb mid-retry, actually
 * stopped the session (since `open` was still true) and then wiped the
 * transcript on the next start(). A disconnect now surfaces a visible
 * "tap to continue" prompt (SurgBotPanel.tsx) that calls retry() — which
 * reconnects without touching transcript/toolEvents/reviewDocument, unlike
 * start()'s genuinely-fresh-session reset. Fails closed on mic permission
 * denial: `error` is set and the socket path still proceeds so control
 * messages / the fallback text path keep working, but no fabricated
 * "listening" state is ever shown. */
export function useSurgBotVoice(): SurgBotVoiceResult {
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [currentPhase, setCurrentPhase] = useState<ReviewPhase | null>(null);
  const [phaseLabel, setPhaseLabel] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [toolEvents, setToolEvents] = useState<ToolCallEvent[]>([]);
  const [reviewDocument, setReviewDocument] = useState<ReviewDocument | null>(null);
  const [isListening, setIsListening] = useState(false);
  const [turnInProgress, setTurnInProgress] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const micNodeRef = useRef<AudioWorkletNode | null>(null);
  const playerRef = useRef<PcmChunkPlayer | null>(null);
  // The call_id of the currently-streaming synthesize_speech reply, if
  // any — lets tool_call_finished know it's the one that should mark the
  // active player complete, without a stale-closure read of toolEvents.
  const ttsCallIdRef = useRef<string | null>(null);
  // Whether the CURRENT turn expects a spoken reply — audio turns (real
  // hold-and-release) do; text_turn ones don't (speak=False server-side,
  // plan_v2 §17). turnInProgress otherwise only resets when a reply clip
  // finishes playing, which never happens for a text-only turn — this is
  // what resets it right when the (text-only) reply text itself arrives.
  const expectingAudioRef = useRef(true);
  // Read inside the AudioWorkletNode's onmessage handler, which fires many
  // times per second — a ref (not state) so gating the send doesn't need a
  // re-render on every single audio chunk, only isListening (the React
  // state mirror, for UI) changes on press/release.
  const isHoldingRef = useRef(false);
  const cancelledRef = useRef(true); // true until start() is called
  const sessionIdRef = useRef<string | null>(null);
  const pendingStartRef = useRef<{ caseIds: string[]; reviewerId: string } | null>(null);
  // Per-speaker id of the transcript entry still receiving deltas, so
  // transcript_delta events append into one running row per turn instead of
  // creating a new bubble per chunk. Reset per connection.
  const activeEntryIdRef = useRef<{ user: string | null; model: string | null }>({ user: null, model: null });

  const teardownAudio = useCallback(() => {
    isHoldingRef.current = false;
    micNodeRef.current?.port.close();
    micNodeRef.current?.disconnect();
    micNodeRef.current = null;
    micStreamRef.current?.getTracks().forEach((track) => track.stop());
    micStreamRef.current = null;
    playerRef.current?.stop();
    playerRef.current = null;
    ttsCallIdRef.current = null;
    if (audioCtxRef.current) {
      audioCtxRef.current.close().catch(() => {
        // Already closed — nothing to do.
      });
    }
    audioCtxRef.current = null;
    expectingAudioRef.current = true;
    setIsListening(false);
    setTurnInProgress(false);
  }, []);

  const handleTranscriptDelta = useCallback((msg: TranscriptDeltaMessage) => {
    setTranscript((prev) => {
      const activeId = activeEntryIdRef.current[msg.speaker];
      if (activeId) {
        const idx = prev.findIndex((e) => e.id === activeId);
        if (idx !== -1) {
          const next = [...prev];
          // Confirmed by capturing real Live API transcript events (not
          // assumed): non-final deltas are genuine incremental fragments to
          // append, but the `final: true` event is a full cumulative
          // restatement of the whole turn so far, not a new fragment — so it
          // must REPLACE the accumulated text, not append to it. Appending
          // it (the original, disclosed assumption) visibly duplicated every
          // turn's text on screen.
          const text = msg.final ? msg.text : next[idx].text + msg.text;
          next[idx] = { ...next[idx], text, final: msg.final, at: Date.now() };
          if (msg.final) activeEntryIdRef.current[msg.speaker] = null;
          return next;
        }
      }
      const id = `${msg.speaker}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      if (!msg.final) activeEntryIdRef.current[msg.speaker] = id;
      return [...prev, { id, speaker: msg.speaker, text: msg.text, final: msg.final, at: Date.now() }];
    });
  }, []);

  const handleServerMessage = useCallback(
    (msg: ServerMessage) => {
      switch (msg.type) {
        case "phase_changed":
          setCurrentPhase(msg.phase);
          setPhaseLabel(msg.phase_label);
          break;
        case "tool_call_started": {
          const event: ToolCallEvent = {
            call_id: msg.call_id,
            // Never defaulted to a guess — a missing field renders as a
            // visible "unknown" in the chip itself (SurgBotPanel.tsx), which
            // is the whole point of the disclosure requirement.
            agent_name: msg.agent_name ?? null,
            model_id: msg.model_id ?? null,
            api_surface: (msg.api_surface as ApiSurface | undefined) ?? null,
            tool_name: msg.tool_name,
            args_summary: msg.args_summary,
            status: "in_progress",
            at: Date.now(),
          };
          setToolEvents((prev) => [...prev, event]);
          // Streaming TTS (plan_v2 §16): a fresh player for THIS turn's
          // reply — created here, before any audio chunk has arrived, so
          // the very first chunk has somewhere to enqueue into. Stops
          // (not just replaces) any leftover player from a prior turn.
          // ttsCallIdRef (a ref, not state) is how tool_call_finished below
          // knows THIS is the call to react to, with no stale-closure risk.
          if (msg.tool_name === "synthesize_speech" && audioCtxRef.current) {
            playerRef.current?.stop();
            ttsCallIdRef.current = msg.call_id;
            playerRef.current = new PcmChunkPlayer(audioCtxRef.current, PCM_OUTPUT_SAMPLE_RATE, () => {
              playerRef.current = null;
              setTurnInProgress(false);
            });
          }
          break;
        }
        case "tool_call_finished":
          setToolEvents((prev) =>
            prev.map((e) => (e.call_id === msg.call_id ? { ...e, status: "finished", summary: msg.summary } : e)),
          );
          // The server only ever sends tool_call_finished for
          // synthesize_speech AFTER the last audio chunk — the reliable
          // "no more audio coming this turn" signal (see services/
          // surgbot_service/main.py::_send_turn_to_agent).
          if (msg.call_id === ttsCallIdRef.current) {
            playerRef.current?.markComplete();
            ttsCallIdRef.current = null;
          }
          break;
        case "transcript_delta":
          handleTranscriptDelta(msg);
          // A text-only turn (speak=False server-side) never gets a reply
          // clip to trigger PcmChunkPlayer's onDrained — this is what
          // resets turnInProgress for that case, right as the (only) reply
          // text arrives. Audio turns are untouched here: their reset
          // still waits for the reply to actually finish playing.
          if (msg.speaker === "model" && msg.final && !expectingAudioRef.current) {
            setTurnInProgress(false);
          }
          break;
        case "review_document_ready":
          setReviewDocument({
            review_id: msg.review_id,
            sections: msg.sections,
            approval_status: msg.approval_status as ApprovalStatus,
          });
          break;
        case "error":
          // A failed turn anywhere in the STT -> LLM -> TTS pipeline (no
          // speech detected, transcription failure, agent error, synthesis
          // failure) — reset turnInProgress so the orb doesn't stay stuck
          // "working" forever with nothing left to end it.
          setError(msg.detail);
          setTurnInProgress(false);
          break;
      }
    },
    [handleTranscriptDelta],
  );

  const initAudioCapture = useCallback(() => {
    void (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
        });
        micStreamRef.current = stream;

        const ctx = new AudioContext();
        audioCtxRef.current = ctx;

        await ctx.audioWorklet.addModule(getMicWorkletBlobUrl());
        const source = ctx.createMediaStreamSource(stream);
        const node = new AudioWorkletNode(ctx, MIC_WORKLET_NAME);
        node.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
          // Push-to-talk gate: the worklet runs continuously once attached
          // (Web Audio has no cheap pause/resume for a running graph), but
          // audio is only ever SENT while the user is actively holding —
          // see startTalking/stopTalking below. Each chunk is forwarded to
          // the server AS CAPTURED (plan_v2 §16 — real streaming
          // StreamingRecognize on the server side depends on this: audio
          // has to arrive while the reviewer is still talking for any of
          // that work to overlap their hold duration, and Cloud
          // Speech-to-Text's real per-chunk size limit — confirmed this
          // session, 25600 bytes — rules out accumulating into one big
          // blob and sending it as a single frame on release anyway).
          const ws = wsRef.current;
          if (isHoldingRef.current && ws && ws.readyState === WebSocket.OPEN) ws.send(event.data);
        };
        // AudioWorkletNode.process() only actually runs while the node is
        // reachable from the destination — route through a zero-gain node
        // rather than straight to destination so the mic is captured and
        // streamed without ever being audibly looped back to the surgeon.
        const mute = ctx.createGain();
        mute.gain.value = 0;
        source.connect(node).connect(mute).connect(ctx.destination);
        micNodeRef.current = node;
        // isListening now means "actively holding push-to-talk," not "mic
        // capture is attached" — that's a silent readiness state with
        // nothing to show the user, so it's intentionally NOT surfaced here.
      } catch (err) {
        setError(err instanceof Error ? `Microphone unavailable: ${err.message}` : "Microphone unavailable");
      }
    })();
  }, []);

  const connectRef = useRef<() => void>(() => {});
  connectRef.current = () => {
    const pending = pendingStartRef.current;
    if (!pending || !sessionIdRef.current) return;
    setStatus("connecting");
    setError(null);
    activeEntryIdRef.current = { user: null, model: null };

    let ws: WebSocket;
    try {
      ws = new WebSocket(wsUrlFor(sessionIdRef.current));
    } catch (err) {
      setStatus("disconnected");
      setError(err instanceof Error ? err.message : "Failed to open SurgBot voice socket");
      return;
    }
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => {
      if (cancelledRef.current) {
        ws.close();
        return;
      }
      const startMsg: ClientMessage = {
        type: "session_start",
        case_ids: pending.caseIds,
        reviewer_id: pending.reviewerId,
      };
      ws.send(JSON.stringify(startMsg));
      setStatus("connected");
      setError(null);
      initAudioCapture();
    };

    ws.onmessage = (event: MessageEvent<string | ArrayBuffer>) => {
      if (typeof event.data === "string") {
        try {
          handleServerMessage(JSON.parse(event.data) as ServerMessage);
        } catch {
          setError("Received a malformed control message from SurgBot service");
        }
        return;
      }
      // One raw PCM16 chunk of the reply, roughly one per sentence
      // (plan_v2 §16) — the player for THIS turn was already created in
      // handleServerMessage's tool_call_started handling, before the
      // first chunk could possibly arrive; just enqueue it.
      playerRef.current?.enqueue(event.data);
    };

    ws.onerror = () => {
      // onclose fires right after and carries the actual retry/teardown
      // logic — nothing additional to do here beyond letting it happen.
    };

    ws.onclose = () => {
      wsRef.current = null;
      teardownAudio();
      // A real disconnect (network drop, relay restart) mid-turn can leave
      // a transcript entry still marked non-final — close it out as a
      // finished bubble (not deleting it, not silently continuing it) so a
      // subsequent reconnect starts a genuinely new bubble instead of
      // resuming into a stale one.
      setTranscript((prev) =>
        prev.map((entry) =>
          entry.id === activeEntryIdRef.current.user || entry.id === activeEntryIdRef.current.model
            ? { ...entry, final: true }
            : entry,
        ),
      );
      activeEntryIdRef.current = { user: null, model: null };
      if (cancelledRef.current) return;
      setStatus("disconnected");
      setError((prev) => prev ?? "Connection lost — tap the orb to continue");
    };
  };

  const stop = useCallback(() => {
    cancelledRef.current = true;
    pendingStartRef.current = null;
    const ws = wsRef.current;
    if (ws) {
      if (ws.readyState === WebSocket.OPEN) {
        try {
          const endMsg: ClientMessage = { type: "end_session" };
          ws.send(JSON.stringify(endMsg));
        } catch {
          // Best-effort — the socket is being torn down regardless.
        }
      }
      ws.close();
      wsRef.current = null;
    }
    teardownAudio();
    setStatus("idle");
    setError(null);
  }, [teardownAudio]);

  const start = useCallback((caseIds: string[], reviewerId: string) => {
    cancelledRef.current = false;
    pendingStartRef.current = { caseIds, reviewerId };
    sessionIdRef.current = sessionIdRef.current ?? newSessionId();
    setTranscript([]);
    setToolEvents([]);
    setReviewDocument(null);
    setCurrentPhase(null);
    setPhaseLabel(null);
    setError(null);
    connectRef.current();
  }, []);

  const retry = useCallback(() => {
    if (!pendingStartRef.current) return; // nothing to reconnect to — start() was never called
    cancelledRef.current = false;
    connectRef.current();
  }, []);

  const sendTextTurn = useCallback((text: string) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    expectingAudioRef.current = false;
    const msg: ClientMessage = { type: "text_turn", text };
    ws.send(JSON.stringify(msg));
    setTurnInProgress(true);
  }, []);

  const stopNarration = useCallback(() => {
    // Stops local playback IMMEDIATELY — the whole point is instant
    // response, so this doesn't wait for the server's acknowledgment.
    // markComplete() isn't right here (that means "no more chunks are
    // coming, but let what's already scheduled finish") — stop() cuts
    // audio off mid-clip, which is exactly what "stop" should do.
    playerRef.current?.stop();
    playerRef.current = null;
    ttsCallIdRef.current = null;
    setTurnInProgress(false);
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const msg: ClientMessage = { type: "stop_narration" };
    ws.send(JSON.stringify(msg));
  }, []);

  const startTalking = useCallback(() => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    isHoldingRef.current = true;
    setIsListening(true);
    const msg: ClientMessage = { type: "mic_start" };
    ws.send(JSON.stringify(msg));
  }, []);

  const stopTalking = useCallback(() => {
    isHoldingRef.current = false;
    setIsListening(false);
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    expectingAudioRef.current = true;
    // Every chunk was already sent as it was captured (see node.port.
    // onmessage above) — mic_stop just signals the turn's audio is
    // complete, so the server can finalize its already-in-progress
    // StreamingRecognize call (plan_v2 §16).
    const msg: ClientMessage = { type: "mic_stop" };
    ws.send(JSON.stringify(msg));
    setTurnInProgress(true);
  }, []);

  // Unmount safety net — never leave a mic stream or socket running past the
  // component's own lifetime.
  useEffect(() => {
    return () => {
      cancelledRef.current = true;
      wsRef.current?.close();
      teardownAudio();
    };
  }, [teardownAudio]);

  return {
    status,
    error,
    currentPhase,
    phaseLabel,
    transcript,
    toolEvents,
    reviewDocument,
    isListening,
    isModelSpeaking: turnInProgress,
    start,
    stop,
    retry,
    startTalking,
    stopTalking,
    sendTextTurn,
    stopNarration,
  };
}
