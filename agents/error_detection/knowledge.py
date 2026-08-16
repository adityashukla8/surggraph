"""Clinical error-knowledge scaffold for the Error Detection Agent's sub-agents.

Legitimately hand-authored — same carve-out already established for World
Model's symbolic rules (plan §3.5, §6 Day 6 note). OCHRA (Objective Clinical
Human Reliability Analysis) is fixed external clinical doctrine, not
something derivable from this project's one video. What must never be
hardcoded is the *reasoning* (whether an error is present in a given
window — that's 100% live Gemini inference over real frames, see
agents/error_detection/subagents.py); what's legitimately fixed here is the
*domain-knowledge scaffold* that reasoning is grounded against, mirroring
exactly how CARES itself works (its own ablation shows this knowledge-
embedded prompting drives ~80% of its total accuracy gain over a naive
VLM baseline — this is the part of CARES that matters most to reproduce).

The six categories below are CARES' own published taxonomy (arXiv:2508.08764,
Table 1) — adopted verbatim rather than inventing a new one. `sedmamba_fine_types`
maps each to the real, confirmed fine-grained SEDMamba/OCHRA error codes it
subsumes (partial — 9 of SEDMamba's 24 codes were confirmed via web research
this session; the complete table was not extractable without poppler-utils,
which isn't installed — see plan §8 risk #11. This is a non-blocker: CARES'
6 categories are the actual detection target, and the fine-grained codes are
supporting evidence text only, never a routing key).

`tis`/`cis` (Technical Intricacy Score / Clinical Impact Score, CARES Eq. 1-3)
are PROJECT-AUTHORED — CARES' paper does not publish its own per-category
values. Disclosed as a judgment call, revisitable, same honesty pattern used
everywhere else hand-authored content appears in this project.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from state.schema import ErrorCategory, ExpertiseTier


class ErrorKnowledgeEntry(BaseModel):
    category: ErrorCategory
    definition: str
    normal_indicators: list[str]
    error_indicators: list[str]
    focus_areas: list[str]
    sedmamba_fine_types: list[str] = Field(default_factory=list)
    tis: int = Field(ge=1, le=3)  # Technical Intricacy Score
    cis: int = Field(ge=1, le=3)  # Clinical Impact Score


ERROR_KNOWLEDGE_LIBRARY: dict[ErrorCategory, ErrorKnowledgeEntry] = {
    "multiple_attempts": ErrorKnowledgeEntry(
        category="multiple_attempts",
        definition=(
            "The surgeon re-attempts the same sub-step (e.g. needle piercing, grasp, "
            "throw) without first fully backing out and re-planning — repetition driven "
            "by a failed first attempt, not a deliberate multi-pass technique."
        ),
        normal_indicators=[
            "A single continuous motion completes the sub-step",
            "Any retry is preceded by a clear regrasp/reposition, not a repeated failed motion",
        ],
        error_indicators=[
            "The same piercing/grasp point is attempted 2+ times in quick succession",
            "Instrument tip re-approaches the same tissue target repeatedly without progress",
            "Visible hesitation followed by a repeated, near-identical motion",
        ],
        focus_areas=["needle tip trajectory", "grasp/release timing", "motion repetition over the window"],
        sedmamba_fine_types=["E1"],
        tis=1,
        cis=1,
    ),
    "out_of_view": ErrorKnowledgeEntry(
        category="out_of_view",
        definition=(
            "The needle or an active instrument leaves the camera's field of view while "
            "still in use, or an instrument is present in-frame but its working tip is "
            "not visible when it should be for the current sub-step."
        ),
        normal_indicators=[
            "Working tips of all active instruments remain in frame",
            "Brief off-screen movement is a deliberate camera/instrument reposition, not mid-task",
        ],
        error_indicators=[
            "Needle disappears from frame while a throw/pass is in progress",
            "An instrument's tip is occluded or off-frame during active tissue interaction",
        ],
        focus_areas=["needle visibility", "instrument tip position relative to frame edges"],
        sedmamba_fine_types=["E3", "E4"],
        tis=2,
        cis=2,
    ),
    "needle_handling": ErrorKnowledgeEntry(
        category="needle_handling",
        definition=(
            "Errors in acquiring, orienting, or maintaining control of the suture needle — "
            "incorrect grasp angle, dropping/slipping the needle while it's in tissue, or "
            "grasping too close to the needle tip for a stable, controlled pass."
        ),
        normal_indicators=[
            "Needle grasped at or near the swage/mid-body with a stable, perpendicular-ish angle",
            "Needle stays under instrument control through the full pass",
        ],
        error_indicators=[
            "Needle visibly slips or drops while partially embedded in tissue",
            "Needle grasped very close to the tip, or at a steep/awkward angle relative to the driver",
            "Needle orientation changes unexpectedly mid-pass without deliberate repositioning",
        ],
        focus_areas=["needle-driver jaw position on the needle", "needle angle at tissue entry", "grip stability through the pass"],
        sedmamba_fine_types=["E2", "E6", "E11"],
        tis=2,
        cis=3,
    ),
    "tissue_handling": ErrorKnowledgeEntry(
        category="tissue_handling",
        definition=(
            "Instrument-tissue interaction that risks or causes direct tissue trauma — "
            "excessive traction, crushing grasps, or forceful contact beyond what the "
            "current sub-step requires."
        ),
        normal_indicators=[
            "Grasping force and traction are proportionate to the tissue and task",
            "Tissue returns to its resting position/color after instrument release",
        ],
        error_indicators=[
            "Visible blanching, tenting, or tearing at a grasp point",
            "Traction direction/force appears disproportionate to what the sub-step requires",
            "Repeated forceful re-grasping of the same tissue region",
        ],
        focus_areas=["tissue deformation under traction", "grasp force relative to tissue type", "post-release tissue appearance"],
        sedmamba_fine_types=["E5"],
        tis=2,
        cis=3,
    ),
    "suture_handling": ErrorKnowledgeEntry(
        category="suture_handling",
        definition=(
            "Errors in managing the suture material itself once passed — entanglement, "
            "excessive slack or tension, or a throw that doesn't lie correctly."
        ),
        normal_indicators=[
            "Suture runs cleanly from spool/tail to the current throw with no visible loops",
            "Tension is even and appropriate for the tissue being approximated",
        ],
        error_indicators=[
            "Visible loop, twist, or entanglement in the suture line",
            "Suture is noticeably slack or over-tensioned relative to the tissue gap",
            "The surgeon pauses to manually clear/untangle suture mid-task",
        ],
        focus_areas=["suture line path across frames", "tension at the current throw", "entanglement near the working area"],
        sedmamba_fine_types=["E19"],
        tis=3,
        cis=2,
    ),
    "instrument_control": ErrorKnowledgeEntry(
        category="instrument_control",
        definition=(
            "General loss of precise instrument control not specific to the needle or "
            "suture — instruments clashing with each other, imprecise or jerky movement, "
            "or poor coordination between the two working arms."
        ),
        normal_indicators=[
            "Both instruments move with smooth, deliberate trajectories",
            "Adequate spacing maintained between simultaneously-active instruments",
        ],
        error_indicators=[
            "Visible clash or collision between the two instrument arms",
            "Jerky, overshooting, or corrective micro-movements beyond normal tremor filtering",
            "One instrument idles awkwardly in a way that obstructs the other's working path",
        ],
        focus_areas=["inter-instrument spacing", "trajectory smoothness", "bimanual coordination"],
        sedmamba_fine_types=["E24"],
        tis=2,
        cis=2,
    ),
}


def get_error_knowledge_entry(category: ErrorCategory) -> ErrorKnowledgeEntry:
    return ERROR_KNOWLEDGE_LIBRARY[category]


def compute_psi(category: ErrorCategory) -> int:
    """Ψ(e) = TIS(e) + CIS(e), CARES Eq. 1."""
    entry = get_error_knowledge_entry(category)
    return entry.tis + entry.cis


def route_tier(psi: int) -> ExpertiseTier:
    """CARES Eq. 3's exact tier bucketing: Ψ∈{2,3}->resident, Ψ∈{4,5}->attending, Ψ=6->expert."""
    if psi in (2, 3):
        return "resident"
    if psi in (4, 5):
        return "attending"
    if psi == 6:
        return "expert"
    raise ValueError(f"psi={psi} is out of CARES' defined range [2,6] (TIS+CIS, each in [1,3])")


def render_knowledge_block(categories: list[ErrorCategory] | None = None) -> str:
    """Renders a compact, prompt-ready text block. `categories=None` (pass 1,
    screening) renders all 6; a single-category list (pass 2, deep/risk-routed)
    renders just that one category's entry."""
    selected = categories if categories is not None else list(ERROR_KNOWLEDGE_LIBRARY.keys())
    blocks = []
    for cat in selected:
        entry = get_error_knowledge_entry(cat)
        blocks.append(
            f"## {cat}\n"
            f"Definition: {entry.definition}\n"
            f"Normal indicators: {'; '.join(entry.normal_indicators)}\n"
            f"Error indicators: {'; '.join(entry.error_indicators)}\n"
            f"Focus areas: {'; '.join(entry.focus_areas)}"
        )
    return "\n\n".join(blocks)
