"""Shared video-metadata and frame-sampling helpers.

FPS is always read from the actual video file (cv2), never assumed — see
scripts/compute_phase_priors.py and tools/sedmamba_labels.py, both of which
need a real frame rate to convert between frame numbers and seconds.

Two ways to get video content into a Gemini call:
  - `build_video_window_content` (native video, GCS-hosted, via
    `types.VideoMetadata`) — real, benchmarked, still complete and working
    (docs/monitor_agent_video_input_benchmark.md: ~2.7x fewer prompt
    tokens and caught a real event still-frame sampling missed, at ~2.9x
    higher per-call latency), but not currently called by any agent as of
    docs/latency_optimization.md's second pass — Monitor's deep tier and
    Scene Graph Builder both moved to still frames for latency. Left in
    place for a future need, not deleted.
  - `sample_frames`/`frames_to_gemini_parts`/`build_multimodal_content`
    (locally-extracted JPEG stills) — what every agent actually uses now.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np
from dotenv import load_dotenv
from google.cloud import storage as gcs_storage
from google.genai import types

load_dotenv()

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
VIDEO_DIR = DATA_ROOT / "video"

# Single source of truth for the real-time sweep window size, shared by
# Monitor, Scene Graph Builder, and Anticipation (docs/latency_optimization.md)
# — config-driven rather than hardcoded independently in each agent module,
# so tuning it can't leave them out of sync with each other. Override via
# SURGGRAPH_WINDOW_S in .env; 5.0 matches the second latency pass's real,
# measured choice, not an arbitrary default.
DEFAULT_WINDOW_S = float(os.environ.get("SURGGRAPH_WINDOW_S", "5.0"))

_VIDEO_MIME_TYPES = {
    ".avi": "video/x-msvideo",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


# Deterministic source-file preference, not directory-iteration order.
# Real bug this fixes: once scripts/prepare_demo_videos.py's transcoded
# and composited-overlay MP4s exist alongside the original .avi,
# `iterdir()`'s arbitrary order could return whichever file the filesystem
# happened to list first — including a still-being-written file (a
# genuine "moov atom not found" cv2 read failure was observed from this).
# The suffix priority order picks the original downloaded source first
# (canonical frame numbering the real annotations were built against);
# "_annotated" is always excluded — it's a UI-display derivative, never a
# source agents should read frames from.
_SOURCE_SUFFIX_PRIORITY = [".avi", ".mov", ".mp4"]


def _find_source_video(video_id: str) -> Path | None:
    video_dir = VIDEO_DIR / video_id
    if not video_dir.exists():
        return None
    candidates = {
        p for p in video_dir.iterdir() if p.suffix.lower() in _VIDEO_MIME_TYPES and "_annotated" not in p.stem
    }
    for suffix in _SOURCE_SUFFIX_PRIORITY:
        matches = sorted(p for p in candidates if p.suffix.lower() == suffix)
        if matches:
            return matches[0]
    return None


def find_video_fps(video_id: str) -> float | None:
    path = _find_source_video(video_id)
    if path is None:
        return None
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    return fps if fps > 0 else None


def find_video_path(video_id: str) -> Path | None:
    return _find_source_video(video_id)


def find_video_duration_s(video_id: str) -> float | None:
    """Real total duration, read directly from the source file (frame count
    / fps) — used as the honest default upper bound for a sweep when a
    caller doesn't supply an explicit end_s (agents/scene_graph_builder)."""
    path = _find_source_video(video_id)
    if path is None:
        return None
    cap = cv2.VideoCapture(str(path))
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    if frame_count <= 0 or fps <= 0:
        return None
    return frame_count / fps


def format_video_time(seconds: float) -> str:
    """m:ss, matching the native <video> player's own time display
    (VideoPanel shows "0:00 / 4:31") — so a time shown on the graph and a
    time shown on the video tile read as the same clock, both derived from
    the real fps, never a hardcoded frame rate. Extracted here once both
    agents/monitor and agents/scene_graph_builder needed the identical
    formatting — real reuse, not speculative."""
    total = max(0, round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def format_video_time_range(start_s: float, end_s: float) -> str:
    return f"{format_video_time(start_s)}–{format_video_time(end_s)}"


class VideoWindow(NamedTuple):
    window_id: str
    start_s: float
    end_s: float
    start_frame: int
    end_frame: int


def generate_nonoverlapping_windows(start_s: float, end_s: float, window_s: float, fps: float, id_prefix: str) -> list[VideoWindow]:
    """Simple non-overlapping chunks across [start_s, end_s) — real frame
    boundaries derived from the real fps, never hardcoded frame counts.
    Shared by Scene Graph Builder and Anticipation, whose windowing needs
    are identical (unlike Monitor's heavy-overlap CARES-style grid, which
    stays its own thing in tools/sedmamba_labels.py::generate_windows)."""
    windows: list[VideoWindow] = []
    t = start_s
    idx = 0
    while t < end_s:
        w_end = min(t + window_s, end_s)
        windows.append(
            VideoWindow(
                window_id=f"{id_prefix}-w{idx:04d}",
                start_s=t,
                end_s=w_end,
                start_frame=round(t * fps),
                end_frame=round(w_end * fps),
            )
        )
        t += window_s
        idx += 1
    return windows


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


def video_mime_type(path_or_uri: str) -> str:
    return _VIDEO_MIME_TYPES.get(Path(path_or_uri).suffix.lower(), "video/mp4")


def find_video_gcs_uri(video_id: str) -> str | None:
    """Finds the uploaded video's GCS URI (gs://<bucket>/videos/<video_id>/<filename>)
    for native video input to Gemini — mirrors find_video_path's local-file
    lookup, against Cloud Storage instead. Requires SURGGRAPH_GCS_BUCKET to
    be set and the video to have already been uploaded there (a one-time
    step per video, not per call/window/agent — see
    docs/monitor_agent_video_input_benchmark.md)."""
    bucket_name = os.environ.get("SURGGRAPH_GCS_BUCKET")
    if not bucket_name:
        return None
    client = gcs_storage.Client()
    prefix = f"videos/{video_id}/"
    video_blobs = [
        b for b in client.list_blobs(bucket_name, prefix=prefix) if Path(b.name).suffix.lower() in _VIDEO_MIME_TYPES
    ]
    if not video_blobs:
        return None
    return f"gs://{bucket_name}/{video_blobs[0].name}"


def build_video_window_content(
    gcs_uri: str,
    mime_type: str,
    start_s: float,
    end_s: float,
    fps: float,
    instruction_text: str,
    role: str = "user",
    extra_parts: list[types.Part] | None = None,
) -> types.Content:
    """Native-video equivalent of build_multimodal_content: clips directly
    to [start_s, end_s) via Gemini's own VideoMetadata rather than
    extracting/encoding still frames locally. `fps` controls sampling
    density (valid range (0.0, 24.0], Gemini default 1.0) — see
    agents/monitor/subagents.py::VIDEO_FPS_PROFILE for the per-role values
    chosen after benchmarking (docs/monitor_agent_video_input_benchmark.md).
    `extra_parts`, if given, are appended after the video part — e.g.
    agents/scene_graph_builder's real segmentation-mask image, a second
    real signal alongside the video window itself."""
    return types.Content(
        role=role,
        parts=[
            types.Part.from_text(text=instruction_text),
            types.Part(
                file_data=types.FileData(file_uri=gcs_uri, mime_type=mime_type),
                video_metadata=types.VideoMetadata(fps=fps, start_offset=f"{start_s:.1f}s", end_offset=f"{end_s:.1f}s"),
            ),
            *(extra_parts or []),
        ],
    )
