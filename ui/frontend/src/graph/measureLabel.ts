// Real text measurement for the graph's variable-width nodes.
//
// plan_v2 §4.1 specifies nodes with a fixed height whose length grows to fit
// the label, with neighbors repositioning so nothing overlaps. Dagre can only
// deliver the "neighbors reposition" half if it is told each node's REAL
// rendered width — feeding it one constant (as a fixed-width design does)
// makes its spacing math wrong the moment nodes differ in size, and the
// overlap it is supposed to prevent shows up anyway.
//
// So we measure with a canvas 2D context using the same font the node
// actually renders in. This is exact rather than an average-char-width
// estimate, and it costs one cached canvas plus a map lookup per label.

// Must stay in sync with .case-node / .case-node__label / .case-node__icon in
// App.css. A mismatch here doesn't throw — it silently degrades dagre's
// spacing, which is the failure mode this module exists to prevent.
const LABEL_FONT = '500 12px "Poppins", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
const HORIZONTAL_PADDING = 10 * 2; // .case-node padding: 8px 10px
const BORDER = 1.5 * 2;
const ICON_WIDTH = 11; // .case-node__icon font-size
const ICON_GAP = 6; // .case-node__row gap

/** Enough room for the icon plus a couple of characters — below this a node
 *  reads as a glyph rather than a labeled thing. */
const MIN_NODE_WIDTH = 130;

/** A ceiling, not a design target. Agent prompts already constrain labels to
 *  short phrases, so real labels land far below this; it exists only so one
 *  pathological output can't blow out the whole layout. */
const MAX_NODE_WIDTH = 520;

let ctx: CanvasRenderingContext2D | null | undefined;
const cache = new Map<string, number>();

function getContext(): CanvasRenderingContext2D | null {
  if (ctx === undefined) {
    // Cached across calls including the null result — in a non-DOM context
    // (SSR, tests) we want to fall back once, not retry per label.
    ctx = document.createElement("canvas").getContext("2d");
    if (ctx) ctx.font = LABEL_FONT;
  }
  return ctx;
}

/** The node's real rendered width in px, clamped to [MIN, MAX]. */
export function measureNodeWidth(label: string): number {
  const cached = cache.get(label);
  if (cached !== undefined) return cached;

  const context = getContext();
  // Fallback when no canvas exists: 6.6px/char approximates Poppins 500 12px
  // closely enough to keep layout sane. Deliberately an approximation, and
  // only ever reached outside a real browser.
  const textWidth = context ? context.measureText(label).width : label.length * 6.6;

  const total = textWidth + ICON_WIDTH + ICON_GAP + HORIZONTAL_PADDING + BORDER;
  const width = Math.round(Math.min(MAX_NODE_WIDTH, Math.max(MIN_NODE_WIDTH, total)));

  cache.set(label, width);
  return width;
}

/** Fixed, per §4.1 — only length varies. */
export const NODE_HEIGHT = 56;
