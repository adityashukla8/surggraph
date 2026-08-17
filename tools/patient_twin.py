"""The synthetic patient twin — docs/plan_v2_autonomous_safety_system.md §2.

Loaded once per case and referenced by downstream reasoning as prior context:
it is what makes complication reasoning specific to a patient rather than
generic ("what could go wrong here" vs. "what could go wrong for a 64-year-old
with a 46 mL prostate and partial nerve-sparing intent").

DISCLOSURE IS STRUCTURAL, NOT COSMETIC. This profile is authored, not real, and
that fact travels with the data: `synthetic: True` sits in the graph node's
attrs, the profile carries its own `_disclosure` string, and the label says so.
The standing project rule is that a viewer must never mistake fabricated data
for a real signal — synthetic input that is openly labeled is legitimate; the
same data presented as a real record would not be.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from state import node_ids
from state.schema import GraphNodePatch
from tools.state_tools import apply_state_patch

PROFILE_PATH = Path(__file__).resolve().parent.parent / "data" / "synthetic" / "patient_twin.json"

_cache: dict[str, Any] | None = None


def load_patient_twin() -> dict[str, Any]:
    """Reads the profile from disk once per process.

    Deliberately no fallback: if the file is missing or malformed this raises
    rather than substituting defaults. A silently-defaulted patient profile
    would make every downstream complication judgment quietly wrong about the
    patient it is reasoning over.
    """
    global _cache
    if _cache is None:
        with open(PROFILE_PATH) as f:
            _cache = json.load(f)
    return _cache


def damico_risk(profile: dict[str, Any] | None = None) -> str:
    """D'Amico risk group, DERIVED from PSA, Gleason and clinical stage.

    Not a stored field, because storing it would let it drift out of step with
    the three values it is defined by. The criteria are the published ones
    (D'Amico et al., JAMA 1998): high if PSA > 20 or Gleason >= 8 or stage
    >= T2c; intermediate if PSA 10-20 or Gleason 7 or T2b; low otherwise.
    """
    p = profile or load_patient_twin()
    psa = p["prostate"]["psa_ng_ml"]
    gleason = p["prostate"]["gleason_score"]
    stage = p["prostate"]["clinical_stage"].lower().removeprefix("c")
    primary, _, secondary = gleason.partition("+")
    gleason_sum = int(primary) + int(secondary or 0)

    if psa > 20 or gleason_sum >= 8 or stage >= "t2c":
        return "high"
    if psa >= 10 or gleason_sum == 7 or stage == "t2b":
        return "intermediate"
    return "low"


def elevated_prior_fields(profile: dict[str, Any] | None = None) -> dict[str, str]:
    """{dotted field path: why it matters}. Declared in the profile data rather
    than decided in the UI, so the marker dots and the risk summary read from
    one place and cannot disagree."""
    p = profile or load_patient_twin()
    return {e["field"]: e["reason"] for e in p.get("elevated_priors", [])}


def risk_profile_summary(profile: dict[str, Any] | None = None) -> str:
    """Two or three sentences naming the clinically meaningful priors.

    Composed from the profile's own declared elevated priors rather than
    written out as fixed prose, so editing the patient changes the summary
    instead of leaving a stale sentence describing a patient who no longer
    exists. Deterministic — no model call for a sentence about static data.
    """
    p = profile or load_patient_twin()
    priors = {e["field"] for e in p.get("elevated_priors", [])}

    difficulty = []
    if "prostate.volume_ml" in priors:
        difficulty.append(f"large prostate volume ({p['prostate']['volume_ml']} mL)")
    if "prostate.median_lobe" in priors:
        difficulty.append("median lobe")
    if "surgical_plan.prior_turp" in priors:
        difficulty.append("prior TURP")
    if "demographics.bmi" in priors:
        difficulty.append(f"BMI {p['demographics']['bmi']}")

    sentences = []
    if difficulty:
        sentences.append(f"Elevated technical difficulty: {', '.join(difficulty)}.")

    anastomosis = []
    if "prostate.membranous_urethra_length_mm" in priors:
        anastomosis.append(f"short membranous urethra ({p['prostate']['membranous_urethra_length_mm']} mm)")
    if p["surgical_plan"]["nerve_sparing"].startswith("bilateral"):
        anastomosis.append("bilateral nerve-sparing intent")
    if anastomosis:
        sentences.append(f"{', '.join(anastomosis).capitalize()} raises anastomosis complexity and continence risk.")

    if "anesthetic_risk.asa_class" in priors:
        sentences.append(f"ASA {p['anesthetic_risk']['asa_class']} — reduced physiological reserve.")

    # An honest empty state rather than a reassuring sentence nobody checked.
    return " ".join(sentences) or "No elevated technical-difficulty priors recorded for this patient."


def summarize_for_prompt(profile: dict[str, Any] | None = None) -> str:
    """A compact prose rendering for reasoning prompts.

    Prose rather than raw JSON because this goes into a clinical-reasoning
    prompt alongside frames and graph context, where a readable sentence costs
    fewer tokens than a nested object and reads as what it is. Leads with the
    synthetic disclosure so the model never treats it as a retrieved record.
    """
    p = profile or load_patient_twin()
    demo = p["demographics"]
    prostate = p["prostate"]
    plan = p["surgical_plan"]

    flags = ", ".join(p["comorbidity_flags"]) or "none recorded"
    return (
        f"SYNTHETIC patient profile (illustrative, not a real record). "
        f"{demo['age_years']}-year-old, BMI {demo['bmi']}, ASA class {p['anesthetic_risk']['asa_class']}. "
        f"Prostate volume {prostate['volume_ml']} mL, PSA {prostate['psa_ng_ml']} ng/mL, "
        f"Gleason {prostate['gleason_score']}, clinical stage {prostate['clinical_stage']}. "
        f"Planned: {plan['procedure']} with {plan['nerve_sparing'].replace('_', ' ')} nerve-sparing and "
        f"{plan['lymph_node_dissection'].replace('_', ' ')} lymph node dissection, "
        f"{plan['positioning'].replace('_', ' ')} positioning at {plan['pneumoperitoneum_target_mmhg']} mmHg pneumoperitoneum. "
        f"Comorbidities: {flags}. "
        f"D'Amico risk group: {damico_risk(p)}. "
        f"Elevated priors the reasoning layer should weigh: "
        f"{'; '.join(e['reason'] for e in p.get('elevated_priors', [])) or 'none recorded'}"
    )


def patient_twin_attrs(profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything the twin node carries.

    Extracted because the orchestrator builds the twin node inline as part of
    its batched skeleton write, while this module has its own writer — and the
    two silently drifted: derived fields added here never reached the graph,
    because the path that actually runs was the other one. One builder now, so
    that cannot recur.
    """
    p = profile or load_patient_twin()
    return {
        # Carried on the node itself so any consumer — UI panel, context slice,
        # documentation draft — sees the disclosure without having to look it up.
        "synthetic": True,
        "disclosure": p["_disclosure"],
        "profile": p,
        "prompt_summary": summarize_for_prompt(p),
        # Derived once here rather than in each consumer, so the panel, the
        # reasoning prompts and the operative note cannot disagree.
        "damico_risk": damico_risk(p),
        "elevated_prior_fields": elevated_prior_fields(p),
        "risk_profile_summary": risk_profile_summary(p),
    }


async def write_patient_twin_node(case_id: str) -> str:
    """Writes the twin onto the graph as part of the case's static skeleton.

    Drawn up front at case open alongside the agent hierarchy (docs §6 step 1),
    before any sweep starts, so it is already present when the first
    complication-reasoning slice needs it — rather than being fetched mid-flight
    while the thread pool is saturated with Gemini calls.
    """
    profile = load_patient_twin()
    node_id = node_ids.patient_twin()

    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=node_id,
            node_type="patient_twin",
            label=f"{profile['display_name']} (synthetic)",
            attrs=patient_twin_attrs(profile),
            source_agent="orchestrator",
            source_tool="write_patient_twin_node",
        ),
        reason="Synthetic patient profile loaded for this case",
        source_agent="orchestrator",
        source_tool="write_patient_twin_node",
    )
    return node_id
