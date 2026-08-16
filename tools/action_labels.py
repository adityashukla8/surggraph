"""Real per-frame action/phase lookup from SAR-RARP50's `action_continuous.txt`
(the same real, run-length-encoded file `scripts/compute_phase_priors.py`
computes transition priors from). Phase IDs are the dataset's own action
IDs, as strings, matching `data/priors/phase_transition_matrix.json`'s
`phases` list convention — never invented labels.

Used by agents/error_detection/agent.py to attach a real `phase` value to each
DivergenceEvent until the real Scene Graph Builder agent exists to derive
phase live from Gemini vision (at which point this becomes the validation-
sidecar comparison target for that agent instead).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
ANNOTATIONS_DIR = DATA_ROOT / "annotations"


@dataclass(frozen=True)
class ActionSegment:
    start_frame: int
    end_frame: int
    action_id: int


def load_action_segments(video_id: str) -> list[ActionSegment]:
    path = ANNOTATIONS_DIR / video_id / "action_continuous.txt"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    segments = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        start_s, end_s, action_s = line.split(",")
        segments.append(ActionSegment(int(start_s), int(end_s), int(action_s)))
    return segments


def phase_at_frame(video_id: str, frame_number: int, segments: list[ActionSegment] | None = None) -> str | None:
    segs = segments if segments is not None else load_action_segments(video_id)
    for seg in segs:
        if seg.start_frame <= frame_number <= seg.end_frame:
            return str(seg.action_id)
    return None
