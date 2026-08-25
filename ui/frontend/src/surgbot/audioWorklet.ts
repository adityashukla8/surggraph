// Mic capture (-> 16kHz mono PCM16, out to the server) and playback (<- 16kHz
// mono PCM16, in from the server) for SurgBot's voice path. Live API requires
// exactly this format both directions (plan_v2 §14.4).
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
// The playback side is deliberately NOT a worklet: queueing whole
// AudioBufferSourceNodes back-to-back against a running `nextStartTime`
// cursor is the simplest thing that plays arriving PCM16 chunks glitch-free
// and continuously, and needs no cross-thread messaging at all.

export const MIC_WORKLET_NAME = "surgbot-mic-processor";
// Live API's input/output rates are asymmetric — confirmed against Google's
// own docs, not assumed: mic input is 16kHz, but the model's spoken audio
// comes back at 24kHz. Playing 24kHz-sampled PCM through an AudioContext
// buffer declared at 16kHz stretches playback ~1.5x slower and drops the
// pitch by the same factor — this was the real cause of a reported "slow
// motion, thick, robotic voice" that didn't change when the voice_name was
// changed (changing which voice speaks can't fix a wrong playback rate,
// which distorts every voice identically).
export const PCM_SAMPLE_RATE = 16000; // mic capture / Live API input rate
export const PCM_OUTPUT_SAMPLE_RATE = 24000; // Live API's spoken-audio output rate

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

/** Schedules incoming 16kHz mono PCM16 chunks back-to-back against a running
 *  cursor so playback is continuous even though chunks arrive in bursts
 *  separated by network jitter. Each `enqueue` call is O(1) relative to
 *  history — nothing here re-plays or re-buffers earlier audio. */
export class PcmPlayer {
  private ctx: AudioContext;
  private sampleRate: number;
  private nextStartTime = 0;
  private activeSources = new Set<AudioBufferSourceNode>();
  private onQueueDrained?: () => void;

  constructor(ctx: AudioContext, sampleRate: number = PCM_SAMPLE_RATE, onQueueDrained?: () => void) {
    this.ctx = ctx;
    this.sampleRate = sampleRate;
    this.onQueueDrained = onQueueDrained;
  }

  enqueue(pcm16: ArrayBuffer): void {
    if (pcm16.byteLength === 0) return;
    const int16 = new Int16Array(pcm16);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) {
      const v = int16[i];
      float32[i] = v < 0 ? v / 0x8000 : v / 0x7fff;
    }

    const buffer = this.ctx.createBuffer(1, float32.length, this.sampleRate);
    buffer.copyToChannel(float32, 0);

    const source = this.ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.ctx.destination);

    const now = this.ctx.currentTime;
    const startAt = Math.max(now, this.nextStartTime);
    source.start(startAt);
    this.nextStartTime = startAt + buffer.duration;

    this.activeSources.add(source);
    source.onended = () => {
      this.activeSources.delete(source);
      if (this.activeSources.size === 0) this.onQueueDrained?.();
    };
  }

  /** Stops everything immediately and resets the schedule cursor — used when
   *  a session ends or reconnects, so stale audio from a torn-down connection
   *  never keeps playing into the next one. */
  stop(): void {
    for (const source of this.activeSources) {
      try {
        source.stop();
      } catch {
        // Already stopped/ended — nothing to do.
      }
    }
    this.activeSources.clear();
    this.nextStartTime = 0;
  }

  get isPlaying(): boolean {
    return this.activeSources.size > 0;
  }
}
