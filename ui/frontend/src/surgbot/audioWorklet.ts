// Mic capture (-> 16kHz mono PCM16, out to the server) for SurgBot's classic
// STT -> LLM -> TTS voice path (plan_v2 §15). Cloud Speech-to-Text's
// ExplicitDecodingConfig wants exactly this format (agents/surgbot/
// speech.py::transcribe_audio).
//
// The capture side is a real AudioWorkletProcessor, registered via
// audioContext.audioWorklet.addModule(). It's shipped as an inline source
// string turned into a Blob URL rather than a separate static asset file —
// AudioWorklet modules load from a URL, not from an ES import, and a Blob URL
// avoids needing a public/ asset + a second build entry point for one small
// processor. The processor itself does a simple linear-interpolation
// resample from the hardware's sample rate down to 16kHz and converts
// Float32 -> Int16, carrying a leftover fractional tail across process()
// calls so the resample is continuous across block boundaries rather than
// re-basing (and audibly clicking) every ~2.7ms render quantum.
//
// Playback is a single decoded clip per turn, not a continuous stream —
// the old chunk-streaming PcmPlayer (queueing PCM16 fragments back-to-back
// against a running cursor) no longer applies now that the server sends one
// complete synthesized WAV per reply instead of a live audio stream. See
// playAudioClip() below.

export const MIC_WORKLET_NAME = "surgbot-mic-processor";
export const PCM_SAMPLE_RATE = 16000; // mic capture / Cloud Speech-to-Text input rate

const MIC_PROCESSOR_SOURCE = `
class SurgBotMicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._targetRate = ${PCM_SAMPLE_RATE};
    this._carry = new Float32Array(0);
  }

  process(inputs) {
    const input = inputs[0];
    const channel = input && input[0];
    if (!channel || channel.length === 0) return true;

    // sampleRate is a global provided inside AudioWorkletGlobalScope — the
    // context's real hardware rate, not necessarily 48000.
    const ratio = sampleRate / this._targetRate;

    const src = new Float32Array(this._carry.length + channel.length);
    src.set(this._carry, 0);
    src.set(channel, this._carry.length);

    const outLength = Math.max(0, Math.floor((src.length - 1) / ratio));
    const out = new Int16Array(outLength);
    for (let i = 0; i < outLength; i++) {
      const pos = i * ratio;
      const i0 = Math.floor(pos);
      const frac = pos - i0;
      const s0 = src[i0];
      const s1 = i0 + 1 < src.length ? src[i0 + 1] : s0;
      const sample = s0 + (s1 - s0) * frac;
      const clamped = Math.max(-1, Math.min(1, sample));
      out[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }

    // Keep whatever tail of src wasn't consumed by the last output sample so
    // the next block's interpolation picks up exactly where this one left off.
    const consumedThrough = outLength > 0 ? (outLength - 1) * ratio : -1;
    const carryStart = Math.max(0, Math.floor(consumedThrough) + 1);
    this._carry = src.slice(carryStart);

    if (out.length > 0) {
      this.port.postMessage(out.buffer, [out.buffer]);
    }
    return true;
  }
}
registerProcessor(${JSON.stringify(MIC_WORKLET_NAME)}, SurgBotMicProcessor);
`;

let cachedBlobUrl: string | null = null;

/** Returns a stable Blob URL for the mic processor module. Cached for the
 *  page's lifetime — audioWorklet.addModule() is idempotent per URL per
 *  context, but there's no reason to mint a fresh Blob on every session. */
export function getMicWorkletBlobUrl(): string {
  if (cachedBlobUrl) return cachedBlobUrl;
  const blob = new Blob([MIC_PROCESSOR_SOURCE], { type: "application/javascript" });
  cachedBlobUrl = URL.createObjectURL(blob);
  return cachedBlobUrl;
}

export interface AudioClipHandle {
  /** Stops playback immediately if still in progress (e.g. ending the
   *  session mid-reply) — safe to call even after the clip already
   *  finished on its own. */
  stop(): void;
}

/** Decodes and plays ONE complete synthesized reply clip (plan_v2 §15 — the
 *  server now sends one full WAV per turn via Cloud Text-to-Speech, not a
 *  continuous stream of PCM16 fragments, so there's no scheduling cursor to
 *  maintain). decodeAudioData handles the real container format (WAV,
 *  confirmed empirically against agents/surgbot/speech.py's actual output —
 *  see tests/test_surgbot_speech.py) without this file needing to know the
 *  exact byte layout. */
export function playAudioClip(ctx: AudioContext, data: ArrayBuffer, onEnded?: () => void): AudioClipHandle {
  let source: AudioBufferSourceNode | null = null;
  let stopped = false;

  ctx
    .decodeAudioData(data.slice(0))
    .then((buffer) => {
      if (stopped) return;
      source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      source.onended = () => {
        source = null;
        onEnded?.();
      };
      source.start();
    })
    .catch(() => {
      // Undecodable clip — treat playback as immediately ended rather than
      // leaving the caller's "SurgBot is speaking" state stuck forever.
      onEnded?.();
    });

  return {
    stop(): void {
      stopped = true;
      if (source) {
        try {
          source.stop();
        } catch {
          // Already stopped/ended — nothing to do.
        }
        source = null;
      }
    },
  };
}
