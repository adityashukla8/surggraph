// Color assignment for the Living Graph.
//
// TWO AXES, deliberately on different visual channels so they coexist rather
// than compete:
//
//   node kind  -> the node's OUTLINE color (and dash pattern)
//   agent id   -> the node's ICON color
//
// This is the reading of docs/plan_v2_autonomous_safety_system.md §4.1, which
// specifies each node kind's color as its "edge color" and calls corrective
// trajectories "dotted-outline yellow edge color nodes" — i.e. the node's own
// boundary, not the graph edges attached to it. Outline for kind, icon for
// provenance, means a viewer can answer "what kind of thing is this" and
// "who produced it" from the same node without either signal fighting the
// other.
//
// Per the dataviz skill, categorical hues come from the fixed 8-slot palette
// and are never cycled past it. Color is always a secondary cue here — every
// node also carries its label as text and a kind icon — so it is redundant
// reinforcement, not the sole identity channel.

import type { EdgeKind, NodeType } from "./types";

// --- Axis 1: node kind -> outline -------------------------------------------
// Kinds §4.1 leaves unspecified use --baseline (the neutral default) rather
// than an invented hue. Perception and phase nodes are explicitly "default
// color" in the spec, which is the same thing.

const NODE_KIND_COLOR: Record<NodeType, string> = {
  // Structural
  trigger: "var(--baseline)",
  agent: "var(--baseline)",
  patient_twin: "var(--baseline)",
  // Perception + phase — spec says default
  entity: "var(--baseline)",
  perception_event: "var(--baseline)",
  snapshot: "var(--baseline)",
  phase: "var(--baseline)",
  vitals: "var(--baseline)",
  manual_event: "var(--baseline)",
  // Reasoning chain
  error: "var(--series-2)", // orange
  complication: "var(--series-2)", // orange
  literature_evidence: "var(--series-1)", // blue
  corrective_trajectory: "var(--series-4)", // yellow, dotted (see NODE_KIND_OUTLINE_STYLE)
  divergence_alert: "var(--series-8)", // red
  // Action + safety
  action_intent: "var(--baseline)",
  verification_block: "var(--series-6)", // green
  action_outcome: "var(--baseline)",
  // Post-case
  benchmark: "var(--node-brown)",
  documentation: "var(--node-brown)",
};

export function nodeKindColorVar(nodeType: NodeType): string {
  return NODE_KIND_COLOR[nodeType] ?? "var(--baseline)";
}

// Corrective trajectories are the one kind the spec calls out as needing a
// distinct outline STYLE, not just a hue — they are normative proposals about
// what should happen, so they must not read as something that did happen.
const DOTTED_OUTLINE_KINDS: ReadonlySet<NodeType> = new Set<NodeType>(["corrective_trajectory"]);

export function nodeKindOutlineStyle(nodeType: NodeType): "solid" | "dotted" {
  return DOTTED_OUTLINE_KINDS.has(nodeType) ? "dotted" : "solid";
}

// --- Axis 2: agent identity -> icon -----------------------------------------

export const AGENT_COLOR_ORDER: readonly string[] = [
  "orchestrator", // --series-1 blue
  "perception", // --series-2 orange
  "anticipation", // --series-3 aqua
  "complication_reasoning", // --series-4 yellow
  "corrective_replanning", // --series-5 magenta
  "divergence_detection", // --series-6 green
  "verification_gate", // --series-7 violet
  "error_detection", // --series-8 red
];

const SERIES_VARS = [
  "--series-1",
  "--series-2",
  "--series-3",
  "--series-4",
  "--series-5",
  "--series-6",
  "--series-7",
  "--series-8",
] as const;

const OTHER_AGENT_VAR = "--text-muted";

export function agentColorVar(agentName: string): string {
  // Error Detection's real source_agent values are
  // "error_detection_coordinator" / "_temporal" / "_spatial" / "_procedural"
  // (one shared family, four real node identities) — none match the bare
  // "error_detection" entry via exact comparison, so without normalization all
  // four silently fall through to the muted "other agent" color instead of
  // their shared hue.
  const normalized = agentName.startsWith("error_detection") ? "error_detection" : agentName;
  const idx = AGENT_COLOR_ORDER.indexOf(normalized);
  return idx === -1 ? `var(${OTHER_AGENT_VAR})` : `var(${SERIES_VARS[idx]})`;
}

// --- Edges ------------------------------------------------------------------
// Edge kind carries meaning primarily through line style (dash pattern +
// weight); color is secondary reinforcement. The dashed kinds are exactly the
// two §4.2 marks dashed — prediction and proposal — because both describe
// something that has not happened yet.

export const DASHED_EDGE_KINDS: ReadonlySet<EdgeKind> = new Set<EdgeKind>(["prediction", "proposal"]);

export const EDGE_KIND_COLOR: Record<EdgeKind, string> = {
  hierarchy: "var(--baseline)", // the static case skeleton — deliberately quiet
  involved: "var(--baseline)", // perception_event -> entity, very high volume
  detection: "var(--series-2)", // perception -> error/divergence (orange, matches error nodes)
  causal_reasoning: "var(--series-2)", // error -> complication
  evidence: "var(--series-1)", // literature -> complication/corrective (blue)
  prediction: "var(--text-secondary)", // recolored per-agent inline where the originating agent is known
  proposal: "var(--series-4)", // toward a corrective trajectory (yellow, matches those nodes)
  trajectory_comparison: "var(--series-8)", // actual vs. proposed (red, matches divergence alerts)
  confirmation: "var(--series-6)", // a prediction reconciled against reality (green)
  verification: "var(--series-6)", // fail-closed gate outcome (green)
  outcome: "var(--baseline)", // action_intent -> its real delivery result
  grading: "var(--series-7)", // post-case only, predicted -> ground truth (violet)
  succession: "var(--text-secondary)", // the chronological spine — present but never loud
};

export const CONFIRMATION_STATUS_COLOR: Record<string, string> = {
  pending: "var(--status-warning)",
  confirmed: "var(--status-good)",
  refuted: "var(--text-muted)", // greyed, not a status hue — a refuted prediction isn't an alarm
};

// --- Icons ------------------------------------------------------------------
// Shape is the one channel that survives both color-blindness and a greyscale
// screenshot, so each family gets a visually distinct glyph.

export const NODE_TYPE_ICON: Record<NodeType, string> = {
  // Structural
  trigger: "⏵",
  agent: "◆",
  patient_twin: "☗",
  // Perception + phase
  entity: "■",
  perception_event: "▫", // small and quiet — these are the highest-volume node kind
  snapshot: "◉",
  phase: "●",
  vitals: "∿",
  manual_event: "✎", // human-authored, never disguised as an agent's inference
  // Reasoning chain
  error: "▲", // alarm-shaped, deliberately
  complication: "◈",
  literature_evidence: "❝",
  corrective_trajectory: "⇢", // points at what should happen next
  divergence_alert: "⚠",
  // Action + safety
  action_intent: "◇", // hollow — proposed, not yet done
  verification_block: "⛉",
  action_outcome: "✦", // the node that reached the real world
  // Post-case
  benchmark: "▦",
  documentation: "▤",
};
