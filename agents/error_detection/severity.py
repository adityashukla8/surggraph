"""Error severity — what the complication-reasoning trigger filters on.

docs/agentic_workflow.md has Complication Reasoning firing on "any error node
above severity threshold", but error nodes carried only `confidence` and
`composite_score`, neither of which is severity: a detector can be very
confident about a trivial error, and unsure about a dangerous one. This module
defines severity explicitly rather than leaving the trigger to stand in for it.

BUILT FROM TWO SIGNALS THAT ALREADY EXIST, not from anything new:

  CLINICAL IMPACT — CIS from the OCHRA knowledge library, each category's
  intrinsic clinical weight on [1,3]. This is literally what CIS means, and it
  is what makes an out-of-view instrument and a needle-handling error rank
  differently even when detected with identical confidence.

  DETECTION STRENGTH — how far the weighted composite cleared the firing
  threshold. A category that barely crossed is a weaker claim than one that
  cleared it comfortably, and a corrective proposal built on a marginal
  detection should be easier to hold back.

DISCLOSED AS PROJECT-AUTHORED. The blend weight and the two band cutoffs below
are judgment calls, exactly like DEFAULT_ALPHA and DEFAULT_THRESHOLD in
aggregation.py — CARES publishes neither a severity scale nor per-category
values. They are exposed as module constants so they can be re-tuned against
real validation data rather than buried in an expression.
"""

from __future__ import annotations

from typing import Literal

from agents.error_detection.knowledge import get_error_knowledge_entry
from state.schema import ErrorCategory

SeverityBand = Literal["low", "medium", "high"]

# Clinical impact leads. A high-impact category detected marginally still
# deserves attention; a low-impact one detected emphatically mostly does not.
CLINICAL_IMPACT_WEIGHT = 0.65
DETECTION_STRENGTH_WEIGHT = 0.35

# Band cutoffs on the resulting [0,1] score.
MEDIUM_BAND_MIN = 0.40
HIGH_BAND_MIN = 0.70

# The margin at which detection strength is considered saturated: a composite
# this much above threshold contributes full weight, and beyond it nothing
# further. Without a cap, one very loud detection would dominate the blend and
# effectively erase the clinical-impact term.
DETECTION_MARGIN_SATURATION = 0.5


def detection_strength(composite_score: float, threshold: float) -> float:
    """Normalized [0,1] measure of how decisively the composite cleared the bar."""
    if threshold <= 0:
        return 0.0
    margin = (composite_score - threshold) / threshold
    if margin <= 0:
        return 0.0
    return min(1.0, margin / DETECTION_MARGIN_SATURATION)


def clinical_impact(category: ErrorCategory) -> float:
    """CIS on [1,3], normalized to [0,1]."""
    return (get_error_knowledge_entry(category).cis - 1) / 2.0


def severity_score(category: ErrorCategory, composite_score: float, threshold: float) -> float:
    return round(
        CLINICAL_IMPACT_WEIGHT * clinical_impact(category)
        + DETECTION_STRENGTH_WEIGHT * detection_strength(composite_score, threshold),
        3,
    )


def severity_band(score: float) -> SeverityBand:
    if score >= HIGH_BAND_MIN:
        return "high"
    if score >= MEDIUM_BAND_MIN:
        return "medium"
    return "low"


def assess(category: ErrorCategory, composite_score: float, threshold: float) -> tuple[float, SeverityBand]:
    """Returns (score, band) for an error that has already fired."""
    score = severity_score(category, composite_score, threshold)
    return score, severity_band(score)


# Complication reasoning triggers at this band and above. Set at "medium" so a
# low-impact category that barely cleared the detection threshold does not
# spend a literature retrieval and a reasoning call on it — while anything with
# real clinical weight does, even when detected marginally.
COMPLICATION_TRIGGER_BAND: SeverityBand = "medium"

_BAND_ORDER: dict[SeverityBand, int] = {"low": 0, "medium": 1, "high": 2}


def meets_complication_trigger(band: SeverityBand) -> bool:
    return _BAND_ORDER[band] >= _BAND_ORDER[COMPLICATION_TRIGGER_BAND]
