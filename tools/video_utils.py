"""Shared video-metadata and frame-sampling helpers.

FPS is always read from the actual video file (cv2), never assumed — see
scripts/compute_phase_priors.py and tools/sedmamba_labels.py, both of which
need a real frame rate to convert between frame numbers and seconds.

Frame sampling + Gemini multi-image encoding is used by the Monitor Agent's
sub-agents (agents/monitor/subagents.py) to build the multi-image calls
CARES-style windowed detection needs (plan §3.5).
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np
from google.genai import types

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
VIDEO_DIR = DATA_ROOT / "video"


def find_video_fps(video_id: str) -> float | None:
    video_dir = VIDEO_DIR / video_id
    if not video_dir.exists():
        return None
    video_files = [p for p in video_dir.iterdir() if p.suffix.lower() in {".avi", ".mp4", ".mov"}]
    if not video_files:
        return None
    cap = cv2.VideoCapture(str(video_files[0]))
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    return fps if fps > 0 else None


def find_video_path(video_id: str) -> Path | None:
    video_dir = VIDEO_DIR / video_id
    if not video_dir.exists():
        return None
    video_files = [p for p in video_dir.iterdir() if p.suffix.lower() in {".avi", ".mp4", ".mov"}]
    return video_files[0] if video_files else None


class FrameSample(NamedTuple):
    frame_number: int
    jpeg_bytes: bytes


def sample_frames(
    video_path: Path | str,
    start_frame: int,
    end_frame: int,
    n_frames: int,
    resize_to: tuple[int, int] | None = None,
    jpeg_quality: int = 90,
) -> list[FrameSample]:
    """Reads `n_frames` evenly spaced frames (inclusive of both endpoints)
    between start_frame and end_frame, returning them as JPEG-encoded bytes
    in chronological order. Uses cv2's own encoder directly on the BGR array
    cv2.VideoCapture returns — no Pillow, no manual BGR->RGB conversion,
    which avoids a classic color-swap bug (a JPEG encoded via cv2.imencode
    is already a correct, displayable JPEG regardless of cv2's internal BGR
    channel order)."""
    cap = cv2.VideoCapture(str(video_path))
    try:
        frame_numbers = np.linspace(start_frame, end_frame, n_frames, dtype=int)
        samples: list[FrameSample] = []
        for frame_number in frame_numbers:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_number))
            ok, frame = cap.read()
            if not ok:
                continue
            if resize_to is not None:
                frame = cv2.resize(frame, resize_to)
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
            if not ok:
                continue
            samples.append(FrameSample(frame_number=int(frame_number), jpeg_bytes=encoded.tobytes()))
        return samples
    finally:
        cap.release()


def frames_to_gemini_parts(frames: list[FrameSample]) -> list[types.Part]:
    return [types.Part.from_bytes(data=f.jpeg_bytes, mime_type="image/jpeg") for f in frames]


def build_multimodal_content(instruction_text: str, frames: list[FrameSample], role: str = "user") -> types.Content:
    """One Content with a text Part (instructions + a frame-index/timestamp
    caption line per image, so the model knows which frame is which) plus
    N image Parts, in chronological order."""
    caption_lines = "\n".join(f"Frame {i + 1}/{len(frames)}: video frame #{f.frame_number}" for i, f in enumerate(frames))
    text = f"{instruction_text}\n\n{caption_lines}"
    parts = [types.Part.from_text(text=text), *frames_to_gemini_parts(frames)]
    return types.Content(role=role, parts=parts)
