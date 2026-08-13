import { useEffect, useRef, type RefObject } from "react";

const DRIFT_THRESHOLD_S = 0.25;
// How close a resolved seek needs to be to a value *we* last mirrored onto
// that same element to count as "that mirror finally resolving," rather
// than a genuinely new position — generous because seeks land on a nearby
// keyframe, not the exact requested second.
const MIRROR_MATCH_EPSILON_S = 0.75;
// Periodic steady-state drift check: infrequent and coarse on purpose (see
// point 3 in the module doc below) — this is not the seek-mirroring path.
const PERIODIC_CHECK_MS = 4000;
const PERIODIC_DRIFT_THRESHOLD_S = 1.5;

/** Keeps the raw and annotated video tiles playing the same case in lockstep:
 * play/pause/seek/rate on either one mirrors onto the other.
 *
 * Deliberately event-mirroring rather than a single shared "leader" video
 * driving both <video> elements — either tile can be the one the user
 * clicks play/seek on, and both should follow. A `syncing` guard stops a
 * mirrored action from re-triggering the handler back on its own source
 * within the same synchronous call.
 *
 * Both videos are served over real network byte-range fetches (GCS, not a
 * local file — see services/state_service/gcs_video.py), so a seek can take
 * real seconds to resolve. Two real bugs showed up under that latency,
 * confirmed via manual testing:
 *
 * 1. An earlier version also resynced `currentTime` on every `timeupdate`
 *    tick (~4x/sec). While one video was still buffering after a seek, its
 *    transitional `currentTime` kept nudging the other into re-seeking too,
 *    and neither side could ever finish buffering before being nudged
 *    again — both stalled, cycling across whatever 2-3 frames were already
 *    cached. Fixed by dropping `timeupdate` correction entirely.
 *
 * 2. Even with only `play`/`pause`/`seeked` mirroring: if video A seeks and
 *    resolves quickly, plays, and advances several seconds *before* video
 *    B's mirrored seek (a slower network fetch) finally resolves, B's own
 *    `seeked` event fires with B now at the OLD target time — and
 *    unconditionally mirroring that back would yank A backward to match a
 *    stale position, even though A has since played past it correctly.
 *    Fixed by tracking what value *this hook* last mirrored onto each
 *    element (`lastMirroredTo`): when a `seeked` event's resolved time
 *    matches what we ourselves just set, it's recognized as that mirror
 *    finally landing — not a fresh position — and is not re-propagated.
 *
 * 3. Dropping `timeupdate` correction (point 1) also removed the only thing
 *    keeping steady-state playback in sync: confirmed the raw and annotated
 *    files decode at genuinely different real-time rates under this proxy
 *    (the raw file is a higher bitrate for the same duration, so it needs
 *    more 8MB chunk fetches per second of playback and stalls refilling its
 *    buffer more often — see services/state_service/gcs_video.py's
 *    _MAX_CHUNK_BYTES) — so with zero correction, drift grew unbounded
 *    (measured: 0.3s to 2.6s over 10 real seconds of playback). Fixed with
 *    a coarse periodic check instead of a per-frame one: every 4s, only
 *    while both videos are actively playing and neither is mid-seek, nudge
 *    whichever is behind forward to match whichever is ahead — infrequent
 *    and guarded enough not to reintroduce bug 1's feedback loop. */
export function useSyncedVideos(): [RefObject<HTMLVideoElement | null>, RefObject<HTMLVideoElement | null>] {
  const refA = useRef<HTMLVideoElement | null>(null);
  const refB = useRef<HTMLVideoElement | null>(null);
  const syncing = useRef(false);
  const lastMirroredTo = useRef(new WeakMap<HTMLVideoElement, number>());

  useEffect(() => {
    const a = refA.current;
    const b = refB.current;
    if (!a || !b) return;

    const other = (el: HTMLVideoElement) => (el === a ? b : a);

    function withGuard(fn: () => void) {
      if (syncing.current) return;
      syncing.current = true;
      try {
        fn();
      } finally {
        syncing.current = false;
      }
    }

    function syncPosition(from: HTMLVideoElement, to: HTMLVideoElement) {
      if (to.seeking) return; // don't interrupt a seek already in flight
      if (Math.abs(to.currentTime - from.currentTime) > DRIFT_THRESHOLD_S) {
        lastMirroredTo.current.set(to, from.currentTime);
        to.currentTime = from.currentTime;
      }
    }

    // True if `el`'s just-resolved position matches a value this hook
    // itself mirrored onto it — i.e. this `seeked` is that mirror landing
    // late, not a fresh position that should propagate further.
    function wasOurOwnMirrorResolving(el: HTMLVideoElement): boolean {
      const target = lastMirroredTo.current.get(el);
      if (target === undefined) return false;
      lastMirroredTo.current.delete(el);
      return Math.abs(el.currentTime - target) < MIRROR_MATCH_EPSILON_S;
    }

    function onPlay(this: HTMLVideoElement) {
      const target = other(this);
      withGuard(() => {
        syncPosition(this, target);
        void target.play();
      });
    }
    function onPause(this: HTMLVideoElement) {
      withGuard(() => other(this).pause());
    }
    function onSeeked(this: HTMLVideoElement) {
      if (wasOurOwnMirrorResolving(this)) return;
      withGuard(() => syncPosition(this, other(this)));
    }
    function onRateChange(this: HTMLVideoElement) {
      withGuard(() => {
        other(this).playbackRate = this.playbackRate;
      });
    }

    const elements = [a, b];
    for (const el of elements) {
      el.addEventListener("play", onPlay);
      el.addEventListener("pause", onPause);
      el.addEventListener("seeked", onSeeked);
      el.addEventListener("ratechange", onRateChange);
    }

    const driftCheck = setInterval(() => {
      if (syncing.current) return;
      // Only correct while both are genuinely playing and settled — mid-seek
      // is exactly the state that caused bug 1, never nudge into that.
      if (a.paused || b.paused || a.seeking || b.seeking) return;
      const drift = a.currentTime - b.currentTime;
      if (Math.abs(drift) <= PERIODIC_DRIFT_THRESHOLD_S) return;
      const [ahead, behind] = drift > 0 ? [a, b] : [b, a];
      withGuard(() => {
        lastMirroredTo.current.set(behind, ahead.currentTime);
        behind.currentTime = ahead.currentTime;
      });
    }, PERIODIC_CHECK_MS);

    return () => {
      clearInterval(driftCheck);
      for (const el of elements) {
        el.removeEventListener("play", onPlay);
        el.removeEventListener("pause", onPause);
        el.removeEventListener("seeked", onSeeked);
        el.removeEventListener("ratechange", onRateChange);
      }
    };
  }, []);

  return [refA, refB];
}
