"""Tests for the deterministic layer between the perception call and the graph
(docs/plan_v2_autonomous_safety_system.md §7.5-§7.8).

No Gemini calls: this layer is pure and synchronous by design, which is exactly
what makes debounce and rate-ceiling behavior testable at all. Several cases
below replay VERBATIM activity descriptions captured from a real 6-window sweep
against video_01 — the real data that exposed the bug in the original
activity-persistence rule.
"""

from __future__ import annotations

from agents.perception.pipeline import (
    ACTIVITY_CHANGE_MIN_INTERVAL_S,
    ENTITY_ABSENT_WINDOWS_TO_DISAPPEAR,
    HEARTBEAT_INTERVAL_S,
    MAX_ENTITY_EVENTS_PER_WINDOW,
    PerceptionPipeline,
    WindowObservation,
    normalize_activity,
)


def _obs(index, entities=(), relations=(), activity="steady state", phase="2", t=None):
    return WindowObservation(
        window_index=index,
        video_time_s=index * 5.0 if t is None else t,
        entities=[(e, e.replace("_", " "), "instrument", 0.9) for e in entities],
        relations=list(relations),
        activity_description=activity,
        phase_id=phase,
    )


def _kinds(decision):
    return [e.kind for e in decision.events]


# --- Normalization ----------------------------------------------------------


def test_normalization_collapses_wording_and_word_order():
    assert normalize_activity("Dissecting the bladder neck") == normalize_activity("bladder neck dissection")


def test_normalization_distinguishes_genuinely_different_activities():
    assert normalize_activity("Dissecting the bladder neck") != normalize_activity("Suturing the urethral anastomosis")


# --- Steady state must be silent (§7.1) -------------------------------------


def test_unchanged_window_emits_nothing():
    p = PerceptionPipeline()
    p.process(_obs(0, entities=["needle_driver_left"]))
    for i in range(1, 6):
        assert _kinds(p.process(_obs(i, entities=["needle_driver_left"]))) == []


def test_entity_stays_one_node_across_repeat_observations():
    """The registry is bounded at O(distinct objects), not O(windows)."""
    p = PerceptionPipeline()
    for i in range(10):
        p.process(_obs(i, entities=["needle_driver_left"]))
    assert len(p.entities) == 1
    assert p.entities["needle_driver_left"].observation_count == 10


# --- Entity debounce (§7.6) -------------------------------------------------


def test_brief_occlusion_does_not_emit_disappearance():
    """An instrument occluded for fewer than the threshold must not generate a
    disappear/appear pair — occlusion by another instrument is routine."""
    p = PerceptionPipeline()
    p.process(_obs(0, entities=["forceps", "needle_driver"]))
    for i in range(1, ENTITY_ABSENT_WINDOWS_TO_DISAPPEAR):
        assert _kinds(p.process(_obs(i, entities=["needle_driver"]))) == []
    assert p.entities["forceps"].is_active is True


def test_sustained_absence_does_emit_disappearance():
    p = PerceptionPipeline()
    p.process(_obs(0, entities=["forceps", "needle_driver"]))
    kinds = []
    for i in range(1, ENTITY_ABSENT_WINDOWS_TO_DISAPPEAR + 1):
        kinds += _kinds(p.process(_obs(i, entities=["needle_driver"])))
    assert "entity_disappeared" in kinds
    assert p.entities["forceps"].is_active is False
    # The node persists — identity survives leaving the field (§7.2).
    assert "forceps" in p.entities


def test_new_entity_appears_immediately_without_debounce():
    """First appearance is a real event; delaying it would make the graph lag
    the video for no benefit."""
    p = PerceptionPipeline()
    assert "entity_appeared" in _kinds(p.process(_obs(0, entities=["needle_driver"])))


def test_confidence_is_smoothed_not_replaced():
    """A single low-confidence frame nudges the rolling value rather than
    overwriting it (§7.6 EMA)."""
    p = PerceptionPipeline()
    p.process(_obs(0, entities=["needle_driver"]))
    high = p.entities["needle_driver"].confidence_rolling
    obs = _obs(1)
    obs.entities.append(("needle_driver", "needle driver", "instrument", 0.1))
    p.process(obs)
    smoothed = p.entities["needle_driver"].confidence_rolling
    assert 0.1 < smoothed < high


# --- Activity persistence (§7.6) --------------------------------------------


def test_first_activity_is_adopted_immediately():
    p = PerceptionPipeline()
    assert "activity_changed" in _kinds(p.process(_obs(0, activity="suctioning near the prostate")))
    assert p.current_activity_text == "suctioning near the prostate"


def test_single_window_wobble_does_not_change_activity():
    p = PerceptionPipeline()
    p.process(_obs(0, activity="dissecting the bladder neck"))
    p.process(_obs(1, activity="dissecting the bladder neck"))
    assert _kinds(p.process(_obs(2, activity="something else entirely"))) == []
    assert p.current_activity_text == "dissecting the bladder neck"


def test_activity_adopts_when_the_departure_persists_even_if_worded_differently():
    """The regression this rule was rewritten for.

    These are the REAL descriptions Gemini returned for three consecutive
    windows of one continuous suturing activity in a live sweep against
    video_01. All three normalize differently. The original rule required the
    same normalized string twice, so it never fired and the graph's current
    activity stayed None for the entire sweep — a worse failure than the
    flicker the rule exists to prevent. What must persist is the DEPARTURE
    from the established activity, not one exact wording.
    """
    p = PerceptionPipeline()
    p.process(_obs(0, activity="suctioning and exposing the prostate region", t=0.0))
    p.process(_obs(1, activity="observing the surgical field with no instruments visible", t=5.0))
    p.process(_obs(2, activity="suctioning fluid and clearing the field near the prostate", t=10.0))
    decision = p.process(_obs(3, activity="manipulating suture needle with needle drivers", t=15.0))

    assert "activity_changed" in _kinds(decision)
    assert p.current_activity_text == "manipulating suture needle with needle drivers"


# --- Rate ceilings (§7.7) ---------------------------------------------------


def test_activity_change_is_rate_limited_not_dropped():
    """Inside the interval the change is HELD; it fires once the ceiling
    clears, so a genuine second change is delayed rather than lost."""
    p = PerceptionPipeline()
    p.process(_obs(0, activity="first activity", t=0.0))
    p.process(_obs(1, activity="second activity", t=2.0))
    held = p.process(_obs(2, activity="second activity", t=4.0))
    assert _kinds(held) == []
    assert any("rate-limited" in s for s in held.suppressed)

    fired = p.process(_obs(3, activity="second activity", t=ACTIVITY_CHANGE_MIN_INTERVAL_S + 1))
    assert "activity_changed" in _kinds(fired)


def test_entity_event_burst_is_batched_into_one_summary():
    p = PerceptionPipeline()
    many = [f"instrument_{i}" for i in range(MAX_ENTITY_EVENTS_PER_WINDOW + 3)]
    decision = p.process(_obs(0, entities=many))
    assert _kinds(decision).count("entity_appeared") == 0
    assert "state_summary" in _kinds(decision)
    # Nothing is lost — the batched events are carried in the payload.
    summary = next(e for e in decision.events if e.kind == "state_summary")
    assert len(summary.detail["batched"]) == len(many)


# --- Heartbeat (§7.8) -------------------------------------------------------


def test_heartbeat_only_after_sustained_silence():
    p = PerceptionPipeline()
    p.process(_obs(0, entities=["needle_driver"], t=0.0))
    assert p.heartbeat_due(10.0) is False
    assert p.heartbeat_due(HEARTBEAT_INTERVAL_S + 1) is True


def test_heartbeat_carries_the_current_snapshot():
    p = PerceptionPipeline()
    p.process(_obs(0, entities=["needle_driver"], activity="suturing", t=0.0))
    beat = p.build_heartbeat(_obs(12, t=60.0))
    assert beat.detail["heartbeat"] is True
    assert beat.detail["active_entities"] == ["needle_driver"]
    assert beat.detail["current_activity"] == "suturing"


# --- Event sequence ownership -----------------------------------------------


def test_event_seq_is_owned_by_the_pipeline_and_monotonic():
    """Deliberately NOT the state service's write `seq`: reusing that would
    couple node identity to write ordering, so a retried write would land under
    a different node_id than its edges already point at."""
    p = PerceptionPipeline()
    assert [p.next_seq() for _ in range(4)] == [1, 2, 3, 4]
