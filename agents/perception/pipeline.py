"""The deterministic layer between the perception call and the graph.

docs/plan_v2_autonomous_safety_system.md §7.5-§7.7. The reasoning call runs
every window; the graph only hears about it when something actually changed.

WHY THIS EXISTS. Perception at a 5s cadence over a long case produces a raw
output per window that is mostly identical to the previous one — the same
instruments, the same relations, a paraphrase of the same activity. Writing all
of it to the graph turns the reasoning surface into a transcript: the graph
grows without bound, the UI becomes unreadable, and every downstream
event-driven agent fires on noise. The two-tier design (a bounded snapshot of
what is true NOW, plus an append-only stream of what HAPPENED) only works if
something decides which raw outputs are events. That is this module.

WHAT THIS IS NOT. This does not replace, second-guess, or post-hoc "correct"
the model's output. The model still runs every window and its full raw output
is preserved verbatim in the audit log (§7.9). This layer only decides what
gets PROMOTED to a graph event. Every rule here is about emission, never about
inference — no rule changes what was perceived, only whether the graph is told
about it again.

Deliberately pure and synchronous: no I/O, no graph access, no awaits. State
in, decisions out. That makes the debounce and rate-ceiling behavior directly
testable without a running case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Literal

EventKind = Literal[
    "entity_appeared",
    "entity_disappeared",
    "relation_started",
    "relation_ended",
    "relation_burst",
    "activity_changed",
    "phase_changed",
    "state_summary",
]

# --- Debounce (§7.6) --------------------------------------------------------
# An entity that flickers out for one window has almost certainly been occluded
# by another instrument, not removed from the patient. Requiring N consecutive
# windows before believing a change is what stops occlusion from generating a
# disappear/appear pair every few seconds.

ENTITY_ABSENT_WINDOWS_TO_DISAPPEAR = 3
ENTITY_PRESENT_WINDOWS_TO_REAPPEAR = 2
RELATION_PRESENT_OF_LAST_N = (2, 3)  # seen in 2 of the last 3 windows to start
RELATION_ABSENT_WINDOWS_TO_END = 3
ACTIVITY_PERSIST_WINDOWS = 2  # a new activity must hold for 2 windows to count

# Exponential moving average on entity confidence (§7.6). A single low-
# confidence frame — a partially occluded instrument, an odd camera angle —
# should nudge the rolling value, not replace it.
CONFIDENCE_EMA_ALPHA = 0.3

# --- Rate ceilings (§7.7) ---------------------------------------------------
# Backstops, not the primary mechanism. Change-diff and debounce do the real
# work; these exist because a genuinely chaotic segment (several instruments
# swapping at once) would otherwise burst and make the stream unreadable.

ACTIVITY_CHANGE_MIN_INTERVAL_S = 15.0
MAX_ENTITY_EVENTS_PER_WINDOW = 5
RELATION_BURST_THRESHOLD = 3  # >3 relation events for one entity within...
RELATION_BURST_WINDOW_S = 5.0  # ...this span coalesces into one burst event

# --- Heartbeat (§7.8) -------------------------------------------------------
HEARTBEAT_INTERVAL_S = 60.0


_NORMALIZE_STRIP = re.compile(r"[^a-z0-9 ]+")

# Verb families collapsed to one canonical form before comparing activity
# descriptions. Without this the model's ordinary wording drift ("dissecting"
# vs "dissects" vs "performing dissection") reads as a real activity change
# every window, which is the single largest source of spurious events.
_VERB_CANON = {
    "dissects": "dissect",
    "dissecting": "dissect",
    "dissection": "dissect",
    "grasps": "grasp",
    "grasping": "grasp",
    "retracts": "retract",
    "retracting": "retract",
    "retraction": "retract",
    "sutures": "suture",
    "suturing": "suture",
    "cuts": "cut",
    "cutting": "cut",
    "coagulates": "coagulate",
    "coagulating": "coagulate",
    "coagulation": "coagulate",
    "ligates": "ligate",
    "ligating": "ligate",
    "mobilizes": "mobilize",
    "mobilizing": "mobilize",
    "mobilization": "mobilize",
}

_STOPWORDS = frozenset({"the", "a", "an", "is", "are", "of", "to", "and", "with", "on", "in", "being", "currently"})


def normalize_activity(text: str) -> str:
    """Canonical form for comparing two activity descriptions.

    Lowercase, strip punctuation, drop filler words, canonicalize verb forms,
    then sort the remaining tokens — so "Dissecting the bladder neck" and
    "bladder neck dissection" collapse to the same string. Word ORDER is
    deliberately discarded: the model rephrasing the same observation is not a
    surgical event.
    """
    cleaned = _NORMALIZE_STRIP.sub(" ", text.strip().lower())
    tokens = [_VERB_CANON.get(t, t) for t in cleaned.split() if t and t not in _STOPWORDS]
    return " ".join(sorted(set(tokens)))


@dataclass
class EntityState:
    """The registry's view of one entity across the case."""

    stable_id: str
    label: str
    kind: str
    first_seen_window: int
    last_seen_window: int
    observation_count: int = 0
    confidence_rolling: float = 0.0
    is_active: bool = False
    # Debounce counters. Kept as consecutive-run counts rather than a history
    # list — all the rules need is "how long has this been true".
    consecutive_absent: int = 0
    consecutive_present: int = 0


@dataclass
class RelationState:
    key: tuple[str, str, str]  # (subject, verb, target)
    is_active: bool = False
    recent_presence: list[bool] = field(default_factory=list)  # newest last
    consecutive_absent: int = 0


@dataclass
class PendingEvent:
    kind: EventKind
    label: str
    involved_entity_ids: tuple[str, ...] = ()
    detail: dict = field(default_factory=dict)


@dataclass
class WindowObservation:
    """One window's raw perception output, reduced to what the diff needs."""

    window_index: int
    video_time_s: float
    entities: list[tuple[str, str, str, float]]  # (stable_id, label, kind, confidence)
    relations: list[tuple[str, str, str]]  # (subject_id, verb, target_id)
    activity_description: str
    phase_id: str | None


@dataclass
class WindowDecision:
    """What the graph should be told about this window."""

    events: list[PendingEvent]
    entity_updates: list[EntityState]
    activity_changed_to: str | None
    phase_changed_to: str | None
    suppressed: list[str] = field(default_factory=list)  # why things were held back, for the audit log

    @property
    def is_silent(self) -> bool:
        return not self.events


class PerceptionPipeline:
    """Per-case state for the change-diff/debounce/rate-limit rules.

    One instance per case, driven one window at a time in order.
    """

    def __init__(self) -> None:
        self.entities: dict[str, EntityState] = {}
        self.relations: dict[tuple[str, str, str], RelationState] = {}
        self.current_activity_normalized: str | None = None
        self.current_activity_text: str | None = None
        self.current_phase_id: str | None = None
        # Candidate activity awaiting ACTIVITY_PERSIST_WINDOWS confirmation.
        self._pending_activity: tuple[str, str, int] | None = None  # (normalized, text, windows_held)
        self._last_activity_change_s: float | None = None
        self._last_event_s: float | None = None
        self._relation_event_times: dict[str, list[float]] = {}
        self.event_seq = 0

    # --- Entities -----------------------------------------------------------

    def _diff_entities(self, obs: WindowObservation) -> tuple[list[PendingEvent], list[EntityState]]:
        events: list[PendingEvent] = []
        seen_ids = {e[0] for e in obs.entities}

        for stable_id, label, kind, confidence in obs.entities:
            state = self.entities.get(stable_id)
            if state is None:
                # A genuinely new entity. No debounce on first appearance: the
                # first time an instrument enters the field is a real event, and
                # delaying it would make the graph lag the video.
                state = EntityState(
                    stable_id=stable_id,
                    label=label,
                    kind=kind,
                    first_seen_window=obs.window_index,
                    last_seen_window=obs.window_index,
                    observation_count=1,
                    confidence_rolling=confidence,
                    is_active=True,
                    consecutive_present=1,
                )
                self.entities[stable_id] = state
                events.append(
                    PendingEvent(
                        kind="entity_appeared",
                        label=f"{label} entered the field",
                        involved_entity_ids=(stable_id,),
                        detail={"kind": kind, "confidence": confidence},
                    )
                )
                continue

            # Already known: update in place. §7.2 — the node persists across
            # occlusions so the entity keeps one identity for the whole case.
            state.last_seen_window = obs.window_index
            state.observation_count += 1
            state.confidence_rolling = (
                CONFIDENCE_EMA_ALPHA * confidence + (1 - CONFIDENCE_EMA_ALPHA) * state.confidence_rolling
            )
            state.label = label
            state.consecutive_absent = 0
            state.consecutive_present += 1

            if not state.is_active and state.consecutive_present >= ENTITY_PRESENT_WINDOWS_TO_REAPPEAR:
                state.is_active = True
                events.append(
                    PendingEvent(
                        kind="entity_appeared",
                        label=f"{label} re-entered the field",
                        involved_entity_ids=(stable_id,),
                        detail={"kind": kind, "confidence": confidence, "reappearance": True},
                    )
                )

        for stable_id, state in self.entities.items():
            if stable_id in seen_ids:
                continue
            state.consecutive_present = 0
            state.consecutive_absent += 1
            if state.is_active and state.consecutive_absent >= ENTITY_ABSENT_WINDOWS_TO_DISAPPEAR:
                state.is_active = False
                events.append(
                    PendingEvent(
                        kind="entity_disappeared",
                        label=f"{state.label} left the field",
                        involved_entity_ids=(stable_id,),
                        detail={"kind": state.kind, "absent_windows": state.consecutive_absent},
                    )
                )

        return events, list(self.entities.values())

    # --- Relations ----------------------------------------------------------

    def _diff_relations(self, obs: WindowObservation) -> list[PendingEvent]:
        events: list[PendingEvent] = []
        seen = set(obs.relations)

        for key in seen:
            state = self.relations.setdefault(key, RelationState(key=key))
            state.recent_presence.append(True)
            state.recent_presence = state.recent_presence[-RELATION_PRESENT_OF_LAST_N[1] :]
            state.consecutive_absent = 0
            need, of_last = RELATION_PRESENT_OF_LAST_N
            if not state.is_active and sum(state.recent_presence) >= need:
                state.is_active = True
                subject, verb, target = key
                events.append(
                    PendingEvent(
                        kind="relation_started",
                        label=f"{subject} {verb} {target}",
                        involved_entity_ids=(subject, target),
                        detail={"verb": verb},
                    )
                )

        for key, state in self.relations.items():
            if key in seen:
                continue
            state.recent_presence.append(False)
            state.recent_presence = state.recent_presence[-RELATION_PRESENT_OF_LAST_N[1] :]
            state.consecutive_absent += 1
            if state.is_active and state.consecutive_absent >= RELATION_ABSENT_WINDOWS_TO_END:
                state.is_active = False
                subject, verb, target = key
                events.append(
                    PendingEvent(
                        kind="relation_ended",
                        label=f"{subject} stopped {verb} {target}",
                        involved_entity_ids=(subject, target),
                        detail={"verb": verb},
                    )
                )

        return events

    # --- Activity -----------------------------------------------------------

    def _diff_activity(self, obs: WindowObservation) -> tuple[PendingEvent | None, str | None, list[str]]:
        suppressed: list[str] = []
        normalized = normalize_activity(obs.activity_description)
        if not normalized:
            return None, None, suppressed

        if normalized == self.current_activity_normalized:
            self._pending_activity = None  # candidate abandoned; we're back to the established activity
            return None, None, suppressed

        # A different description. Hold it until it persists — a one-window
        # wobble between two phrasings is model variance, not a real change.
        if self._pending_activity and self._pending_activity[0] == normalized:
            held = self._pending_activity[2] + 1
        else:
            held = 1
        self._pending_activity = (normalized, obs.activity_description, held)

        if held < ACTIVITY_PERSIST_WINDOWS:
            suppressed.append(f"activity_changed held: {held}/{ACTIVITY_PERSIST_WINDOWS} windows")
            return None, None, suppressed

        # Rate ceiling: at most one activity change per interval. A genuine
        # second change inside the window is not dropped — the candidate stays
        # pending and fires as soon as the ceiling clears.
        if (
            self._last_activity_change_s is not None
            and obs.video_time_s - self._last_activity_change_s < ACTIVITY_CHANGE_MIN_INTERVAL_S
        ):
            suppressed.append(
                f"activity_changed rate-limited: {obs.video_time_s - self._last_activity_change_s:.0f}s "
                f"< {ACTIVITY_CHANGE_MIN_INTERVAL_S:.0f}s since last change"
            )
            return None, None, suppressed

        previous = self.current_activity_text
        self.current_activity_normalized = normalized
        self.current_activity_text = obs.activity_description
        self._last_activity_change_s = obs.video_time_s
        self._pending_activity = None

        return (
            PendingEvent(
                kind="activity_changed",
                label=obs.activity_description,
                detail={"previous": previous, "normalized": normalized},
            ),
            obs.activity_description,
            suppressed,
        )

    # --- Phase --------------------------------------------------------------

    def _diff_phase(self, obs: WindowObservation) -> tuple[PendingEvent | None, str | None]:
        if obs.phase_id is None or obs.phase_id == self.current_phase_id:
            return None, None
        previous = self.current_phase_id
        self.current_phase_id = obs.phase_id
        return (
            PendingEvent(
                kind="phase_changed",
                label=f"Phase transition at {obs.video_time_s:.0f}s",
                detail={"previous_phase_id": previous, "phase_id": obs.phase_id},
            ),
            obs.phase_id,
        )

    # --- Rate ceilings (§7.7) -----------------------------------------------

    def _apply_ceilings(self, events: list[PendingEvent], obs: WindowObservation) -> tuple[list[PendingEvent], list[str]]:
        suppressed: list[str] = []

        entity_events = [e for e in events if e.kind in ("entity_appeared", "entity_disappeared")]
        relation_events = [e for e in events if e.kind in ("relation_started", "relation_ended")]
        other = [e for e in events if e not in entity_events and e not in relation_events]

        # Coalesce a relation burst on one entity rather than emitting each.
        relation_events, burst_events, burst_note = self._coalesce_relation_bursts(relation_events, obs)
        suppressed.extend(burst_note)

        if len(entity_events) > MAX_ENTITY_EVENTS_PER_WINDOW:
            suppressed.append(
                f"{len(entity_events)} entity events exceeded the per-window ceiling of "
                f"{MAX_ENTITY_EVENTS_PER_WINDOW}; batched into one state_summary"
            )
            summary = PendingEvent(
                kind="state_summary",
                label=f"{len(entity_events)} entities entered/left the field",
                involved_entity_ids=tuple(i for e in entity_events for i in e.involved_entity_ids),
                detail={"batched": [{"kind": e.kind, "label": e.label} for e in entity_events]},
            )
            entity_events = [summary]

        return other + entity_events + relation_events + burst_events, suppressed

    def _coalesce_relation_bursts(
        self, relation_events: list[PendingEvent], obs: WindowObservation
    ) -> tuple[list[PendingEvent], list[PendingEvent], list[str]]:
        suppressed: list[str] = []
        by_entity: dict[str, list[PendingEvent]] = {}
        for event in relation_events:
            for entity_id in event.involved_entity_ids:
                by_entity.setdefault(entity_id, []).append(event)

        bursting: set[str] = set()
        for entity_id, entity_events in by_entity.items():
            times = self._relation_event_times.setdefault(entity_id, [])
            times.extend([obs.video_time_s] * len(entity_events))
            self._relation_event_times[entity_id] = [
                t for t in times if obs.video_time_s - t <= RELATION_BURST_WINDOW_S
            ]
            if len(self._relation_event_times[entity_id]) > RELATION_BURST_THRESHOLD:
                bursting.add(entity_id)

        if not bursting:
            return relation_events, [], suppressed

        kept, coalesced = [], []
        for event in relation_events:
            if any(eid in bursting for eid in event.involved_entity_ids):
                coalesced.append(event)
            else:
                kept.append(event)

        burst_events = []
        if coalesced:
            suppressed.append(
                f"{len(coalesced)} relation events on {sorted(bursting)} coalesced into one relation_burst"
            )
            burst_events.append(
                PendingEvent(
                    kind="relation_burst",
                    label=f"Rapid relation changes involving {', '.join(sorted(bursting))}",
                    involved_entity_ids=tuple(sorted(bursting)),
                    detail={"changes": [{"kind": e.kind, "label": e.label, **e.detail} for e in coalesced]},
                )
            )
        return kept, burst_events, suppressed

    # --- Entry point --------------------------------------------------------

    def process(self, obs: WindowObservation) -> WindowDecision:
        """Diff one window against accumulated state and decide what to emit."""
        entity_events, entity_updates = self._diff_entities(obs)
        relation_events = self._diff_relations(obs)
        activity_event, activity_changed_to, activity_suppressed = self._diff_activity(obs)
        phase_event, phase_changed_to = self._diff_phase(obs)

        events = [*entity_events, *relation_events]
        if activity_event:
            events.append(activity_event)
        if phase_event:
            events.append(phase_event)

        events, ceiling_suppressed = self._apply_ceilings(events, obs)

        if events:
            self._last_event_s = obs.video_time_s

        return WindowDecision(
            events=events,
            entity_updates=entity_updates,
            activity_changed_to=activity_changed_to,
            phase_changed_to=phase_changed_to,
            suppressed=[*activity_suppressed, *ceiling_suppressed],
        )

    def heartbeat_due(self, video_time_s: float) -> bool:
        """True when steady state has run long enough to warrant proving
        liveness (§7.8). Not a per-window "still going" node — one lightweight
        event per HEARTBEAT_INTERVAL_S of genuine silence."""
        if self._last_event_s is None:
            return video_time_s >= HEARTBEAT_INTERVAL_S
        return video_time_s - self._last_event_s >= HEARTBEAT_INTERVAL_S

    def build_heartbeat(self, obs: WindowObservation) -> PendingEvent:
        self._last_event_s = obs.video_time_s
        return PendingEvent(
            kind="state_summary",
            label="Perception steady — no change",
            involved_entity_ids=tuple(sorted(self.active_entity_ids())),
            detail={
                "heartbeat": True,
                "active_entities": sorted(self.active_entity_ids()),
                "current_activity": self.current_activity_text,
                "current_phase_id": self.current_phase_id,
            },
        )

    def active_entity_ids(self) -> Iterable[str]:
        return (e.stable_id for e in self.entities.values() if e.is_active)

    def next_seq(self) -> int:
        """A per-case monotonic counter owned by the pipeline, deliberately NOT
        the state service's write `seq`. Reusing that would couple node
        IDENTITY to write ORDERING, so a retried write would land under a
        different node_id than the one its edges already point at."""
        self.event_seq += 1
        return self.event_seq
