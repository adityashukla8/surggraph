"""Validation-sidecar loader for SEDMamba's real, frame-level surgical error
ground-truth labels.

VALIDATION ONLY — this module must never be imported from Monitor Agent's
live decision path (agents/monitor/coordinator.py, agents/monitor/aggregation.py).
It exists to (a) score the Monitor Agent's live agentic detections against
real ground truth after the fact, and (b) locate a real true-positive window
to feature in the demo. The detection decision itself always comes from live
Gemini reasoning over real frames (see agents/monitor/coordinator.py) — see
plan §3.5 for why the original "fire on this file's label" design was
rejected as a disguised lookup table, not genuine monitoring.

Real file format (confirmed against the actual downloaded data, not
assumed): a pickle per video containing a dict with:
  - 'error_GT': numpy array, binary (0=normal, 1=error), sampled at a rate
     derived below — never assumed to be a fixed Hz value.
  - 'image_name': numpy array of frame filenames (e.g. '000000888.png'),
     zero-padded frame numbers on the SAME numbering scheme as
     action_discrete.txt and segmentation/*.png (real video frame indices).
  - 'feature': DINOv2 embeddings — not used here.

No categorical (24-type) error breakdown is present in this file; only the
binary error/normal signal (see agents/monitor/knowledge.py for how the
6 CARES-published error categories are used instead).
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from tools.video_utils import find_video_fps

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"


def _pkl_path(video_id: str) -> Path:
    return DATA_ROOT / "annotations" / video_id / "error_annotation.pkl"


def _frame_number(image_name: Any) -> int:
    name_str = image_name.decode() if isinstance(image_name, bytes) else str(image_name)
    return int(Path(name_str).stem)


def derive_sample_rate_hz(image_names: np.ndarray, fps: float) -> float:
    """Derives the SEDMamba label sampling rate from the real data itself —
    the spacing between consecutive samples' underlying video frame numbers
    — rather than assuming a fixed Hz value. An earlier attempt hardcoded
    `SEDMAMBA_SAMPLE_RATE_HZ = 5` and was correctly rejected for it; this
    reproduces 5.0 on the real video_01 file, but derived, not assumed.

    Asserts the spacing is constant across the sequence — fails loudly
    rather than silently if a future video's label grid turns out
    irregular, instead of guessing.
    """
    if len(image_names) < 2:
        raise ValueError("need at least 2 samples to derive a sample rate")
    frame_numbers = [_frame_number(n) for n in image_names]
    steps = {b - a for a, b in zip(frame_numbers, frame_numbers[1:])}
    if len(steps) != 1:
        raise ValueError(f"irregular frame spacing in SEDMamba labels: {sorted(steps)} — cannot derive a single rate")
    frame_step = next(iter(steps))
    return fps / frame_step


@dataclass(frozen=True)
class ErrorAnnotations:
    video_id: str
    error_gt: np.ndarray  # shape (n_samples,), binary
    frame_numbers: list[int]  # underlying real video frame number per sample
    sample_rate_hz: float  # derived, see derive_sample_rate_hz
    fps: float  # real, from find_video_fps


def load_error_annotations(video_id: str) -> ErrorAnnotations:
    path = _pkl_path(video_id)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — see data/README.md for how to obtain SEDMamba labels")

    with open(path, "rb") as f:
        obj = pickle.load(f)

    fps = find_video_fps(video_id)
    if fps is None:
        raise FileNotFoundError(f"could not read real FPS for {video_id} — is data/video/{video_id}/ populated?")

    image_names = obj["image_name"]
    sample_rate_hz = derive_sample_rate_hz(image_names, fps)
    frame_numbers = [_frame_number(n) for n in image_names]

    return ErrorAnnotations(
        video_id=video_id,
        error_gt=np.asarray(obj["error_GT"]).astype(int),
        frame_numbers=frame_numbers,
        sample_rate_hz=sample_rate_hz,
        fps=fps,
    )


@dataclass(frozen=True)
class MonitorWindow:
    window_id: str
    start_idx: int  # inclusive, index into ErrorAnnotations.error_gt / frame_numbers
    end_idx: int  # inclusive
    start_frame: int  # real underlying video frame number
    end_frame: int
    start_s: float
    end_s: float


def generate_windows(
    annotations: ErrorAnnotations,
    window_s: float = 10.0,
    stride_s: float = 1.0,
    start_s: float = 0.0,
    end_s: float | None = None,
) -> list[MonitorWindow]:
    """Slides directly in the real 5Hz-derived sample-index space (not raw
    frame or wall-clock space) so window edges always land on a real sample
    — no ambiguity when comparing to ground truth. `start_s`/`end_s` bound
    which windows get generated: omit `end_s` for a full-video sweep, or
    pass a narrow bound for the live demo segment (plan §3.5)."""
    window_samples = round(window_s * annotations.sample_rate_hz)
    stride_samples = round(stride_s * annotations.sample_rate_hz)
    n_samples = len(annotations.error_gt)

    start_idx_bound = round(start_s * annotations.sample_rate_hz)
    end_idx_bound = n_samples if end_s is None else round(end_s * annotations.sample_rate_hz)

    windows: list[MonitorWindow] = []
    idx = start_idx_bound
    while idx + window_samples <= min(n_samples, end_idx_bound):
        end_idx = idx + window_samples - 1
        windows.append(
            MonitorWindow(
                window_id=f"{annotations.video_id}-w{len(windows):04d}",
                start_idx=idx,
                end_idx=end_idx,
                start_frame=annotations.frame_numbers[idx],
                end_frame=annotations.frame_numbers[end_idx],
                start_s=idx / annotations.sample_rate_hz,
                end_s=(end_idx + 1) / annotations.sample_rate_hz,
            )
        )
        idx += stride_samples
    return windows


def window_ground_truth(annotations: ErrorAnnotations, window: MonitorWindow) -> bool:
    """VALIDATION-ONLY — real SEDMamba ground truth for this window (any
    sample in range labeled error=1). Never call this before a Monitor
    detection decision is made; only to score it afterward."""
    return bool(annotations.error_gt[window.start_idx : window.end_idx + 1].any())


def log_window_accuracy(path: Path, record: dict[str, Any]) -> None:
    """Appends one JSON line to the validation log (data/validation/monitor_accuracy.jsonl)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {**record, "logged_at": datetime.now(timezone.utc).isoformat()}
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
