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
import type { AudioClipHandle } from "./audioWorklet";
import { MIC_WORKLET_NAME, getMicWorkletBlobUrl, playAudioClip } from "./audioWorklet";

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
  /** Push-to-talk: startTalking() on press clears the local capture buffer
   *  and begins accumulating mic audio; stopTalking() on release sends the
   *  whole accumulated clip as one binary frame followed by mic_stop,
   *  kicking off the server's real STT -> LLM -> TTS pipeline for that turn
   *  (plan_v2 §15). Mic audio is only ever captured between the two calls. */
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
   *  UX, exposed only for SurgBotPanel's small escape-hatch toggle. */
  sendTextTurn: (text: string) => void;
}

/** Opens the SurgBot voice WebSocket, captures the mic as 16kHz mono PCM16
 *  while held, sends the whole accumulated clip as one binary frame on
 *  release (plan_v2 §15 — classic STT -> LLM -> TTS, not a continuous
 *  stream), decodes and plays the one synthesized reply clip that comes
 *  back, and parses control-channel JSON frames into React state.
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
  const playerRef = useRef<AudioClipHandle | null>(null);
  // Accumulates captured PCM16 chunks for the CURRENT held turn — plan_v2
  // §15 sends the whole clip once on release, not a continuous stream, so
  // chunks are buffered client-side instead of forwarded per-frame.
  const audioChunksRef = useRef<ArrayBuffer[]>([]);
  // Read inside the AudioWorkletNode's onmessage handler, which fires many
  // times per second — a ref (not state) so gating the accumulation doesn't
  // need a re-render on every single audio chunk, only isListening (the
  // React state mirror, for UI) changes on press/release.
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
    audioChunksRef.current = [];
    micNodeRef.current?.port.close();
    micNodeRef.current?.disconnect();
    micNodeRef.current = null;
    micStreamRef.current?.getTracks().forEach((track) => track.stop());
    micStreamRef.current = null;
    playerRef.current?.stop();
    playerRef.current = null;
    if (audioCtxRef.current) {
      audioCtxRef.current.close().catch(() => {
        // Already closed — nothing to do.
      });
    }
    audioCtxRef.current = null;
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
          break;
        }
        case "tool_call_finished":
          setToolEvents((prev) =>
            prev.map((e) => (e.call_id === msg.call_id ? { ...e, status: "finished", summary: msg.summary } : e)),
          );
          break;
        case "transcript_delta":
          handleTranscriptDelta(msg);
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
          // audio is only ever CAPTURED while the user is actively holding —
          // see startTalking/stopTalking below. Chunks are accumulated
          // locally now (plan_v2 §15 sends one whole clip on release), not
          // forwarded to the socket per-frame.
          if (isHoldingRef.current) audioChunksRef.current.push(event.data);
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
      // One complete synthesized reply clip per turn (plan_v2 §15) — decode
      // and play it once, rather than the old continuous PCM16 enqueue.
      if (!audioCtxRef.current) return;
      playerRef.current?.stop();
      playerRef.current = playAudioClip(audioCtxRef.current, event.data, () => {
        playerRef.current = null;
        setTurnInProgress(false);
      });
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
    const msg: ClientMessage = { type: "text_turn", text };
    ws.send(JSON.stringify(msg));
    setTurnInProgress(true);
  }, []);

  const startTalking = useCallback(() => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    audioChunksRef.current = [];
    isHoldingRef.current = true;
    setIsListening(true);
    const msg: ClientMessage = { type: "mic_start" };
    ws.send(JSON.stringify(msg));
  }, []);

  const stopTalking = useCallback(() => {
    isHoldingRef.current = false;
    setIsListening(false);
    const ws = wsRef.current;

    // Concatenate every chunk captured while held into ONE buffer and send
    // it as a single binary frame (plan_v2 §15 — the whole clip at once,
    // not a continuous stream), THEN the mic_stop boundary marker —
    // preserves the same ordering discipline the old streaming protocol
    // already relied on (audio before the turn-complete signal).
    const chunks = audioChunksRef.current;
    audioChunksRef.current = [];
    if (ws && ws.readyState === WebSocket.OPEN && chunks.length > 0) {
      const totalBytes = chunks.reduce((sum, c) => sum + c.byteLength, 0);
      const combined = new Uint8Array(totalBytes);
      let offset = 0;
      for (const chunk of chunks) {
        combined.set(new Uint8Array(chunk), offset);
        offset += chunk.byteLength;
      }
      ws.send(combined.buffer);
    }

    if (!ws || ws.readyState !== WebSocket.OPEN) return;
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
  };
}
