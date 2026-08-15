// Color assignment for the Living State Graph.
//
// Per the dataviz skill: categorical hues are assigned in a FIXED order and
// never cycled past the validated 8-slot palette. We have up to 14 agents,
// more than 8 slots — so only the pipeline agents most visible before/around
// divergence (the ones a viewer actually needs to tell apart at a glance
// during the 4-minute demo) get a dedicated hue; the rest fold into a shared
// muted "other agent" tone. Every node also carries its label as text and a
// node_type icon, so color here is a secondary/redundant cue, not the sole
// identity channel — unlike a scatter plot, mislabeling isn't possible.
export const AGENT_COLOR_ORDER: readonly string[] = [
  "orchestrator", // --series-1 blue
  "scene_graph_builder", // --series-2 orange
  "anticipation", // --series-3 aqua
  "mission_planner", // --series-4 yellow
  "complication_enumeration", // --series-5 magenta
  "world_model", // --series-6 green
  "safety_critic", // --series-7 violet
  "monitor", // --series-8 red
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
  // Monitor's real source_agent values are "monitor_coordinator"/
  // "monitor_temporal"/"monitor_spatial"/"monitor_procedural" (one shared
  // family, four real node identities) — none match the bare "monitor"
  // entry via exact string comparison, so without this normalization all
  // four silently fell through to the muted "other agent" color instead
  // of their intended dedicated hue.
  const normalized = agentName.startsWith("monitor") ? "monitor" : agentName;
  const idx = AGENT_COLOR_ORDER.indexOf(normalized);
  return idx === -1 ? `var(${OTHER_AGENT_VAR})` : `var(${SERIES_VARS[idx]})`;
}

// Edge kind carries meaning primarily through line style (dash pattern +
// weight), matching the context doc's tile-3 spec — color is a secondary
// reinforcement, reserved and never reused for agent identity.
export const EDGE_KIND_COLOR: Record<string, string> = {
  predicted: "var(--text-secondary)", // recolored per-agent inline where the originating agent is known
  action: "var(--baseline)",
  observed: "var(--series-6)", // reserved green — "confirmed real," never used for agent identity
  revised: "var(--series-7)", // reserved violet — post-recovery trajectory only
};

export const CONFIRMATION_STATUS_COLOR: Record<string, string> = {
  pending: "var(--status-warning)",
  confirmed: "var(--status-good)",
  refuted: "var(--text-muted)", // low-opacity/greyed, not a status hue — a refuted prediction isn't an alarm
};

export const NODE_TYPE_ICON: Record<string, string> = {
  agent: "◆", // diamond
  phase: "●", // circle
  entity: "■", // square
  artifact: "✦", // sparkle/star — the node that reached the real world
  event: "▲", // triangle — a detected divergence, deliberately alarm-shaped
};

// "event" nodes (a live-detected divergence) get the status-critical accent
// rather than the neutral baseline other non-agent node types use — this is
// the one node type that should visually read as "something happened here,"
// per plan §3.5's redesign of the Monitor Agent's output.
export const EVENT_NODE_ACCENT = "var(--status-critical)";
