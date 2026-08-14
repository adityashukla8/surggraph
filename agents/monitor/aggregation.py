"""Deterministic risk-routing and weighted-aggregation math for the Monitor
Agent (plan §3.5) — CARES Eq. 1-8, reimplemented in plain Python.

Deliberately NOT an LLM call. The three sub-agents (agents/monitor/subagents.py)
do all the real vision reasoning; this module only combines their already-
decided outputs. Matches the pattern already established for the Anticipation
Agent (get_phase_transition_priors is deterministic computation the LLM
reasons *over*, never arithmetic left to the LLM itself) — letting an LLM
sum three 0/1s against a fixed threshold would be pure hallucination risk
for zero reasoning benefit.
"""

from __future__ import annotations

from state.schema import ErrorCategory, SubAgentRole

# CARES Eq. 7: 𝒞𝒫ₓ(V) = αT·OT,x + αS·OS,x + αP·OP,x, αT > αS > αP required.
# CARES' own baseline sweep used equal weights (α=1.0 each) and doesn't
# publish a final tuned set — these are project-chosen defaults preserving
# the required ordering, on a 0-3 scale. Exposed as a parameter everywhere
# below; re-tune against the real validation sweep (plan §3.5) before demo day.
#
# TODO(deferred, tracked in plan §11 todo list — real data now exists, not
# done yet): the full 261-window offline sweep ran 2026-08-13
# (data/validation/monitor_accuracy.jsonl) — macro_f1=0.408 vs CARES'
# published 0.543 on this same dataset. Confusion: tp=119 fp=18 fn=111 tn=13
# — heavily skewed toward false negatives (111!) relative to false positives
# (18), meaning DEFAULT_THRESHOLD=1.7 is likely too conservative for this
# video's real data and/or these alpha weights underweight the signal that
# actually correlates with true errors here. Re-tuning against this real
# confusion matrix (not guessing) is the next honest step, deferred for now
# in favor of the Orchestrator build.
DEFAULT_ALPHA: dict[SubAgentRole, float] = {
    "temporal": 1.2,
    "spatial": 1.0,
    "procedural": 0.8,
}

# Chosen so that no single agent's dissent alone can fire a divergence
# (max single weight 1.2 < 1.7) but any two agreeing agents can
# (1.2+1.0=2.2, 1.2+0.8=2.0, 1.0+0.8=1.8, all >= 1.7) — a defensible
# "≥2-of-3 agreement" reading of CARES Eq. 8, since CARES doesn't publish
# its own threshold value either (its reported tuned θ=2.25 is specific to
# their 48-video corpus, not something to copy blindly onto a 1-video build).
DEFAULT_THRESHOLD = 1.7

# Below this per-category confidence (from the pass-1 screen), a category is
# not considered a serious enough candidate to escalate to pass-2 risk-routing.
ESCALATION_CONFIDENCE_BAR = 0.4


def pick_escalation_candidate(category_confidences: list[dict[ErrorCategory, float]]) -> ErrorCategory | None:
    """`category_confidences` is one dict per sub-agent (its pass-1 screen
    opinion, category -> confidence, only for categories it formed one on).
    Among categories any agent flagged with confidence >= ESCALATION_CONFIDENCE_BAR,
    picks the one with the highest max-confidence across agents. Returns None
    if nothing clears the bar — that window never gets risk-routed (pass 2
    is skipped, and the screen-pass booleans stand in as the final O values,
    disclosed via MonitorWindowAssessment.psi/tier_used being None)."""
    best_category: ErrorCategory | None = None
    best_confidence = ESCALATION_CONFIDENCE_BAR
    for confidences in category_confidences:
        for category, confidence in confidences.items():
            if confidence >= best_confidence:
                best_confidence = confidence
                best_category = category
    return best_category


def aggregate(
    o_temporal: bool,
    o_spatial: bool,
    o_procedural: bool,
    alpha: dict[SubAgentRole, float] = DEFAULT_ALPHA,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[float, bool]:
    """CARES Eq. 7-8: weighted sum of the three agents' binary calls,
    thresholded to a final decision. Returns (composite_score, is_divergence)."""
    composite = alpha["temporal"] * o_temporal + alpha["spatial"] * o_spatial + alpha["procedural"] * o_procedural
    return composite, composite > threshold
