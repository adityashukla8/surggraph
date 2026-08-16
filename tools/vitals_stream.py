"""Synthetic intraoperative vitals — docs/plan_v2_autonomous_safety_system.md §2.

A synthetic timeline replayed against the video clock, with trends consistent
with the pneumoperitoneum and steep Trendelenburg that RARP actually requires,
plus two scripted excursions that exist to exercise the complication-reasoning
path rather than to decorate a panel.

SYNTHETIC, AND SAID SO EVERYWHERE. Every sample carries `synthetic=True`; the
graph node label says so; the prompt summary leads with it. This is disclosed
synthetic INPUT — legitimate, and structurally different from fabricating a
value to paper over a failure, which the project forbids outright.

WHY IT IS SHAPED, NOT RANDOM. The physiology below is deterministic from the
case clock: the same second of the same case always yields the same sample.
That is what makes a demo reproducible and a divergence explainable. Noise is
present but is itself deterministic (a fixed-seed hash of the timestamp), so
"the vitals moved" is never an artifact of when you happened to look.

Physiological basis for the trends (standard, uncontroversial):
  - Insufflating the abdomen to ~15 mmHg and tilting the patient head-down
    pushes the diaphragm cephalad, so PEAK AIRWAY PRESSURE rises substantially
    and stays elevated for as long as the pneumoperitoneum is up.
  - CO2 is the insufflation gas and is absorbed across the peritoneum, so
    ETCO2 drifts up over the case and does not return to baseline until
    desufflation.
  - Raised intra-abdominal pressure increases systemic vascular resistance, so
    MAP rises modestly on insufflation.
  - Peritoneal stretch carries a vagal component, so HR tends flat-to-slightly-
    down rather than up.
  - SPO2 stays essentially flat in an uncomplicated case; a fall is a real
    signal, which is why one excursion moves it.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

from state import node_ids
from state.schema import GraphNodePatch
from tools.patient_twin import load_patient_twin
from tools.state_tools import apply_state_patch

# --- Case phases as fractions of total duration -----------------------------
# Fractions rather than absolute seconds so the same timeline replays correctly
# against any video length, instead of assuming one clip's duration.

_INDUCTION_END = 0.04  # ports in, insufflation begins
_INSUFFLATION_RAMP_END = 0.10  # pressures/EtCO2 reach their working plateau
_DESUFFLATION_START = 0.95  # pneumoperitoneum released near the end


@dataclass(frozen=True)
class Excursion:
    """A scripted deviation. `label` is what a clinician would call it;
    `rationale` is why it is plausible here, carried through to the graph so a
    viewer can see this was authored deliberately, not sampled from noise."""

    name: str
    start_frac: float
    end_frac: float
    label: str
    rationale: str
    hr_delta: float = 0.0
    map_delta: float = 0.0
    spo2_delta: float = 0.0
    etco2_delta: float = 0.0
    airway_delta: float = 0.0


# Two excursions, per the spec's "a couple of scripted excursions".
EXCURSIONS: tuple[Excursion, ...] = (
    Excursion(
        name="hypotensive_episode",
        start_frac=0.42,
        end_frac=0.52,
        label="Falling MAP with compensatory tachycardia",
        rationale=(
            "Timed to the dissection-heavy middle of the case. A dropping mean arterial pressure with a rising "
            "heart rate is the classic compensated picture for ongoing blood loss, which is exactly the "
            "downstream concern a vascular or tissue-handling error should raise."
        ),
        hr_delta=+22.0,
        map_delta=-19.0,
    ),
    Excursion(
        name="co2_retention",
        start_frac=0.68,
        end_frac=0.78,
        label="Rising EtCO2 and airway pressure with mild desaturation",
        rationale=(
            "Prolonged pneumoperitoneum in steep Trendelenburg progressively worsens CO2 absorption and "
            "respiratory mechanics, more so at elevated BMI. Relevant to how long a corrective plan can "
            "reasonably keep the patient insufflated."
        ),
        etco2_delta=+9.0,
        spo2_delta=-3.0,
        airway_delta=+7.0,
    ),
)

# A sample is flagged as a real deviation only past these margins, so ordinary
# noise never trips the downstream reasoning path.
#
# Measured against the EXPECTED value for this point in the case, not against
# the pre-insufflation baseline. That distinction is load-bearing: insufflation
# legitimately raises EtCO2 by ~9 and peak airway pressure by ~9 for the whole
# case, so comparing to the raw baseline flags both channels continuously, a
# vitals node gets written every window, and the graph floods with the exact
# steady-state noise the perception tier's change-diff design exists to
# suppress. "The abdomen is inflated" is not a deviation; it is the plan.
TREND_THRESHOLDS = {
    "hr_bpm": 15.0,
    "map_mmhg": 12.0,
    "spo2_pct": 2.0,
    "etco2_mmhg": 6.0,
    "peak_airway_pressure_cmh2o": 5.0,
}


@dataclass(frozen=True)
class VitalsSample:
    t_s: float
    hr_bpm: float
    map_mmhg: float
    spo2_pct: float
    etco2_mmhg: float
    peak_airway_pressure_cmh2o: float
    excursion: str | None
    excursion_label: str | None
    deviations: tuple[str, ...]
    synthetic: bool = True

    @property
    def is_excursion(self) -> bool:
        return self.excursion is not None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["deviations"] = list(self.deviations)
        return d

    def summarize_for_prompt(self) -> str:
        base = (
            f"SYNTHETIC vitals at {self.t_s:.0f}s: "
            f"HR {self.hr_bpm:.0f}, MAP {self.map_mmhg:.0f}, SpO2 {self.spo2_pct:.0f}%, "
            f"EtCO2 {self.etco2_mmhg:.0f}, peak airway {self.peak_airway_pressure_cmh2o:.0f} cmH2O."
        )
        if self.excursion_label:
            return f"{base} DEVIATION: {self.excursion_label}."
        return f"{base} Within expected range for pneumoperitoneum + steep Trendelenburg."


def _noise(t_s: float, channel: str, amplitude: float) -> float:
    """Deterministic pseudo-noise: a hash of (channel, whole second) mapped to
    [-amplitude, +amplitude].

    Deterministic on purpose. Real random noise would make the same case
    replay differently every run, so a divergence a viewer saw once could not
    be reproduced — which would make the whole vitals path undebuggable.
    """
    digest = hashlib.sha256(f"{channel}:{int(t_s)}".encode()).digest()
    unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # [0, 1]
    return (unit * 2.0 - 1.0) * amplitude


def _pneumoperitoneum_factor(frac: float) -> float:
    """0 before insufflation, ramping to 1 across induction, back to 0 after
    desufflation. Everything pressure- and CO2-related scales by this."""
    if frac <= _INDUCTION_END:
        return 0.0
    if frac < _INSUFFLATION_RAMP_END:
        return (frac - _INDUCTION_END) / (_INSUFFLATION_RAMP_END - _INDUCTION_END)
    if frac < _DESUFFLATION_START:
        return 1.0
    return max(0.0, 1.0 - (frac - _DESUFFLATION_START) / (1.0 - _DESUFFLATION_START))


def _active_excursion(frac: float) -> Excursion | None:
    for exc in EXCURSIONS:
        if exc.start_frac <= frac < exc.end_frac:
            return exc
    return None


def _excursion_ramp(exc: Excursion, frac: float) -> float:
    """Excursions ramp in and out rather than switching on — a vital sign that
    steps instantaneously reads as a data glitch, not a physiological event."""
    span = exc.end_frac - exc.start_frac
    if span <= 0:
        return 1.0
    position = (frac - exc.start_frac) / span
    # Triangular: peaks at the midpoint of the excursion window.
    return 1.0 - abs(position - 0.5) * 2.0


def expected_at(frac: float) -> dict[str, float]:
    """The uncomplicated course: this patient's physiology at this point in the
    case with no excursion and no noise.

    Split out from `sample_at` because it is the reference the deviation check
    measures against — and because it is genuinely useful on its own, e.g. for
    a UI that wants to draw expected-vs-actual.
    """
    profile = load_patient_twin()
    base = profile["baseline_vitals"]
    pneumo = _pneumoperitoneum_factor(frac)

    # BMI amplifies the airway-pressure cost of insufflation + head-down tilt.
    bmi_burden = max(0.0, (profile["demographics"]["bmi"] - 25.0) / 10.0)

    return {
        "hr_bpm": base["hr_bpm"] - 3.0 * pneumo,
        "map_mmhg": base["map_mmhg"] + 6.0 * pneumo,
        "spo2_pct": base["spo2_pct"] - 0.5 * pneumo,
        # CO2 accumulates with time under insufflation rather than plateauing.
        "etco2_mmhg": base["etco2_mmhg"] + (6.0 + 2.0 * bmi_burden) * pneumo + 3.0 * pneumo * frac,
        "peak_airway_pressure_cmh2o": base["peak_airway_pressure_cmh2o"] + (7.0 + 4.0 * bmi_burden) * pneumo,
    }


def sample_at(t_s: float, case_duration_s: float) -> VitalsSample:
    """The synthetic vitals at a given point on the video clock."""
    frac = 0.0 if case_duration_s <= 0 else max(0.0, min(1.0, t_s / case_duration_s))
    expected = expected_at(frac)

    hr = expected["hr_bpm"]
    mean_ap = expected["map_mmhg"]
    spo2 = expected["spo2_pct"]
    etco2 = expected["etco2_mmhg"]
    airway = expected["peak_airway_pressure_cmh2o"]

    exc = _active_excursion(frac)
    if exc is not None:
        ramp = _excursion_ramp(exc, frac)
        hr += exc.hr_delta * ramp
        mean_ap += exc.map_delta * ramp
        spo2 += exc.spo2_delta * ramp
        etco2 += exc.etco2_delta * ramp
        airway += exc.airway_delta * ramp

    hr += _noise(t_s, "hr", 2.5)
    mean_ap += _noise(t_s, "map", 3.0)
    spo2 += _noise(t_s, "spo2", 0.4)
    etco2 += _noise(t_s, "etco2", 0.8)
    airway += _noise(t_s, "airway", 0.6)

    spo2 = min(100.0, spo2)

    values = {
        "hr_bpm": hr,
        "map_mmhg": mean_ap,
        "spo2_pct": spo2,
        "etco2_mmhg": etco2,
        "peak_airway_pressure_cmh2o": airway,
    }
    # Against the EXPECTED course for this patient at this moment — see
    # TREND_THRESHOLDS. A MAP of 70 means something different for this patient
    # than in general, and an EtCO2 of 44 means something different at minute
    # ten of insufflation than at induction.
    deviations = tuple(
        channel for channel, value in values.items() if abs(value - expected[channel]) >= TREND_THRESHOLDS[channel]
    )

    return VitalsSample(
        t_s=round(t_s, 1),
        hr_bpm=round(hr, 1),
        map_mmhg=round(mean_ap, 1),
        spo2_pct=round(spo2, 1),
        etco2_mmhg=round(etco2, 1),
        peak_airway_pressure_cmh2o=round(airway, 1),
        excursion=exc.name if exc else None,
        excursion_label=exc.label if exc else None,
        deviations=deviations,
    )


async def write_vitals_node(case_id: str, sample: VitalsSample, window_index: int) -> str:
    """Writes a physiological-state node.

    Called only when a window's sample carries a trend flag or excursion (docs
    §6 step 2) — a node per window would flood the graph with steady-state
    noise, which is exactly what the perception tier's change-diff design
    exists to prevent.
    """
    node_id = node_ids.vitals(window_index)
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=node_id,
            node_type="vitals",
            label=f"{sample.excursion_label or 'Vitals deviation'} (synthetic)",
            attrs={"synthetic": True, **sample.to_dict()},
            source_agent="vitals_stream",
            source_tool="write_vitals_node",
        ),
        reason=sample.summarize_for_prompt(),
        source_agent="vitals_stream",
        source_tool="write_vitals_node",
    )
    return node_id


async def update_vitals_snapshot(case_id: str, sample: VitalsSample) -> str:
    """Updates the fixed `snapshot:current_vitals_summary` slot in place.

    A snapshot slot, not an event: its cardinality never grows with case
    length, so "what are the vitals right now" stays one node lookup no matter
    how long the case runs (plan_v2 §7.4).
    """
    node_id = node_ids.SNAPSHOT_CURRENT_VITALS
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=node_id,
            node_type="snapshot",
            label=f"Vitals: HR {sample.hr_bpm:.0f} · MAP {sample.map_mmhg:.0f} · SpO2 {sample.spo2_pct:.0f}% (synthetic)",
            attrs={"synthetic": True, **sample.to_dict()},
            source_agent="vitals_stream",
            source_tool="update_vitals_snapshot",
        ),
        reason=sample.summarize_for_prompt(),
        source_agent="vitals_stream",
        source_tool="update_vitals_snapshot",
    )
    return node_id
