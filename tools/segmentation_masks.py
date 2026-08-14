"""Real SAR-RARP50 segmentation mask handling — shared by
scripts/prepare_demo_videos.py (UI overlay compositing) and
agents/scene_graph_builder (feeding a real mask to Gemini as input
context). Extracted once a second real caller needed the exact same
mechanics, not written speculatively.

Masks are single-channel class-index PNGs (confirmed via direct inspection
of all 272 real mask files for video_01: uint8, classes found = {0..8},
0 = background), one every 60 frames — sparse, not per-frame. No
class-name legend exists anywhere for this dataset (confirmed again by a
repo-wide search) — colors below are for visual consistency only, never a
claim about what a class actually is.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"


def _hex_to_bgr(hex_color: str) -> tuple[int, int, int]:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return (b, g, r)


# Same hex values as ui/frontend/src/index.css's light-mode --series-1..8
# (mirrored here since Python can't read CSS custom properties) — reused
# only for visual consistency with the rest of the app, not a claim about
# class identity. All 8 slots, matching the real 8 non-background classes
# found across every mask file (never a subset chosen from one sample).
_SERIES_HEX = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
CLASS_COLORS_BGR = {class_id: _hex_to_bgr(hex_color) for class_id, hex_color in enumerate(_SERIES_HEX, start=1)}


def load_mask_index(video_id: str) -> dict[int, Path]:
    """frame_number -> real mask file path, sorted, for every real mask
    that actually exists for this video (sparse — do not assume every
    frame number is present)."""
    seg_dir = DATA_ROOT / "annotations" / video_id / "segmentation"
    masks = {}
    for path in seg_dir.glob("*.png"):
        frame_number = int(path.stem)
        masks[frame_number] = path
    return dict(sorted(masks.items()))


def colorize_mask(mask: np.ndarray) -> np.ndarray:
    """mask: single-channel (or repeated-3-channel) class-index array.
    Returns a BGR color image, background (class 0) left black."""
    if mask.ndim == 3:
        mask = mask[:, :, 0]
    color = np.zeros((*mask.shape, 3), dtype=np.uint8)
    for class_id, bgr in CLASS_COLORS_BGR.items():
        color[mask == class_id] = bgr
    return color


def nearest_mask_path(video_id: str, frame_number: int, mask_index: dict[int, Path] | None = None) -> Path | None:
    """The real mask file whose own frame number is closest to
    `frame_number` — never interpolated/synthesized, always one specific
    real annotation file. None if this video has no masks at all."""
    index = mask_index if mask_index is not None else load_mask_index(video_id)
    if not index:
        return None
    nearest_frame = min(index, key=lambda mask_frame: abs(mask_frame - frame_number))
    return index[nearest_frame]


def load_colorized_mask_jpeg(mask_path: Path, jpeg_quality: int = 90) -> bytes:
    """Reads a real mask file, colorizes it, and JPEG-encodes it — ready to
    hand to Gemini as an image Part (e.g. via types.Part.from_bytes)."""
    raw_mask = cv2.imread(str(mask_path), cv2.IMREAD_UNCHANGED)
    if raw_mask is None:
        raise FileNotFoundError(f"could not read mask file: {mask_path}")
    color = colorize_mask(raw_mask)
    ok, encoded = cv2.imencode(".jpg", color, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    if not ok:
        raise RuntimeError(f"failed to JPEG-encode mask: {mask_path}")
    return encoded.tobytes()
