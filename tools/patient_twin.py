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
        f"Comorbidities: {flags}."
    )


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
            attrs={
                # Carried on the node itself so any consumer — UI panel, context
                # slice, documentation draft — sees the disclosure without
                # having to know to look it up.
                "synthetic": True,
                "disclosure": profile["_disclosure"],
                "profile": profile,
                "prompt_summary": summarize_for_prompt(profile),
            },
            source_agent="orchestrator",
            source_tool="write_patient_twin_node",
        ),
        reason="Synthetic patient profile loaded for this case",
        source_agent="orchestrator",
        source_tool="write_patient_twin_node",
    )
    return node_id
