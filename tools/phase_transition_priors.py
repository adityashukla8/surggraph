"""Runtime tool exposing scripts/compute_phase_priors.py's real,
empirically-computed transition priors to the Anticipation Agent via ADK
tool-calling (plan §2) — never a developer-authored transition table.

Called directly by Gemini mid-reasoning (agents/anticipation/subagent.py's
`tools=[get_phase_transition_priors]`), so its signature/docstring double
as the tool's schema — kept plain (str in, dict out) for that reason.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
PRIORS_PATH = DATA_ROOT / "priors" / "phase_transition_matrix.json"


def get_phase_transition_priors(current_phase: str) -> dict:
    """Real, empirically-observed transition statistics for `current_phase`,
    computed once offline (scripts/compute_phase_priors.py) from SAR-RARP50's
    own ground-truth action timeline — never developer-authored. Returns a
    dict with `candidates` (each: next_phase, probability, count,
    expected_duration_s), sorted most-likely first, and `coverage_n` (total
    real observed transitions out of current_phase) — a low coverage_n means
    the statistic is real but thin, not that it's wrong; treat it as a
    starting point to reason from, not an oracle to defer to blindly.
    """
    if not PRIORS_PATH.exists():
        return {
            "current_phase": current_phase,
            "candidates": [],
            "coverage_n": 0,
            "note": "no priors file found — run scripts/compute_phase_priors.py first",
        }
    data = json.loads(PRIORS_PATH.read_text())
    probs: dict[str, float] = data.get("transition_probs", {}).get(current_phase, {})
    counts: dict[str, int] = data.get("transition_counts", {}).get(current_phase, {})
    durations: dict[str, dict] = data.get("duration_stats", {})

    candidates = [
        {
            "next_phase": next_phase,
            "probability": prob,
            "count": counts.get(next_phase, 0),
            "expected_duration_s": durations.get(next_phase, {}).get("mean_s"),
        }
        for next_phase, prob in sorted(probs.items(), key=lambda kv: -kv[1])
    ]
    return {
        "current_phase": current_phase,
        "candidates": candidates,
        "coverage_n": sum(counts.values()),
    }


def summarize_transition_confidence(current_phase: str) -> str:
    """Anonymized version of get_phase_transition_priors' real result — the
    SHAPE of the real statistics (how consistent/thin the historical
    evidence is, real typical duration) with no category label attached.

    Used by agents/anticipation/agent.py to ground its forecast without
    ever handing Gemini an opaque numeric category as if it were an answer
    (plan §13.3): `current_phase` here is a real internal bookkeeping key
    the CALLER (not Gemini) computed — this function's return value is the
    only thing that ever reaches the prompt.
    """
    result = get_phase_transition_priors(current_phase)
    candidates = result["candidates"]
    coverage_n = result["coverage_n"]
    if not candidates:
        return "No historical transition data is available from a comparable point in the procedure."

    top = candidates[0]
    if len(candidates) == 1 or top["probability"] >= 0.85:
        consistency = "highly consistent"
    elif top["probability"] >= 0.6:
        consistency = "moderately consistent"
    else:
        consistency = "historically ambiguous (multiple plausible outcomes observed)"

    duration_note = ""
    if top.get("expected_duration_s") is not None:
        duration_note = f", with the next phase typically lasting around {top['expected_duration_s']:.0f}s"

    return (
        f"Historically, transitions from a comparable point in the procedure have been {consistency} "
        f"({len(candidates)} distinct real outcome(s) observed across {coverage_n} real transition(s) total"
        f"{duration_note})."
    )
