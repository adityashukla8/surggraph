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
# TODO(still deferred — real data exists twice now, still not re-tuned):
# original 261-window sweep (2026-08-13, pre frame-driven restructuring,
# archived at data/validation/monitor_accuracy_2026-08-13_pre_framedriven.jsonl)
# — macro_f1=0.408, confusion tp=119 fp=18 fn=111 tn=13. Rerun 2026-08-15
# after both tiers moved to still frames (53 windows, 5s stride, coarser
# sample — not a strict re-test of the same 262-window set, so treat the
# comparison as directional, not exact) — macro_f1=0.515 (data/validation/
# monitor_accuracy.jsonl), confusion tp=29 fp=2 fn=18 tn=4. Same real
# pattern both times: heavily skewed toward false negatives relative to
# false positives, meaning DEFAULT_THRESHOLD=1.7 is still likely too
# conservative for this video's real data. Re-tuning against the real
# confusion matrix (not guessing) remains the honest next step.
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
