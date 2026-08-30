import { useEffect } from "react";

// SurgBot's container takes ~51s to cold-start (measured p50) and is the one
// backend not pinned to a warm instance. Cloud Run shuts an idle instance
// down after ~15 minutes here (measured over 23 real shutdowns in this
// project), so a single wake-up on page load is not enough: a reviewer who
// works a case for twenty minutes before opening the voice panel would find
// it cold again. Every plausible approach re-arms it instead —
// landing on the site, opening the console, and reaching for the orb.
const SURGBOT_SERVICE_URL = import.meta.env.VITE_SURGBOT_SERVICE_URL ?? "http://127.0.0.1:8091";

// Comfortably inside the observed ~15 minute idle window, so a repeat call
// lands while the instance is still up and simply keeps it up.
const REWARM_AFTER_MS = 10 * 60 * 1000;

// Module-level, not a ref: React mounts effects twice in development, and
// this has to be shared across the three places that call it.
let lastWarmedAt = 0;

/** Wakes SurgBot's container, at most once per re-warm window. */
export function warmSurgBot(): void {
  const now = Date.now();
  if (!SURGBOT_SERVICE_URL || now - lastWarmedAt < REWARM_AFTER_MS) return;
  lastWarmedAt = now;
  // no-cors: the browser still sends a real request, which is all Cloud Run
  // needs to boot the instance, without requiring the service to allow this
  // origin or logging a CORS error for a response nothing reads.
  // /openapi.json is served from memory and opens no case, session or model call.
  void fetch(`${SURGBOT_SERVICE_URL}/openapi.json`, { mode: "no-cors", cache: "no-store" }).catch(() => {
    // A failed wake-up is not worth surfacing — the panel still works, it
    // just pays the cold start when it is opened.
  });
}

export function useWarmSurgBot(): void {
  useEffect(() => warmSurgBot(), []);
}
