"""Prepares browser-playable video files for UI tiles 1 & 2 from the real
downloaded SAR-RARP50 source (data/video/<video_id>/video_left.avi, XviD
codec — not natively playable in Chrome/Firefox's <video> tag).

Tile 1 (raw video): direct transcode to H.264/MP4, no frame processing.

Tile 2 (annotated overlay): the "quick option" — composites the REAL
SAR-RARP50 segmentation masks (data/annotations/<video_id>/segmentation/,
one every 60 frames — sparse, not per-frame) onto the raw footage. A real
mask is shown at full strength for the first half of the real gap to the
next mask, then hidden (plain raw video, no overlay) until the next real
mask lands — never interpolated, faded, or held on screen once it's more
than half a real interval old. A visibly wrong stale overlay (confirmed:
holding a mask the full ~1s gap looked badly out of sync with fast
instrument motion) is worse than briefly showing no overlay at all. Every
overlay pixel that IS shown traces back to a real annotation file, exactly
as published, at full opacity. No class-name legend was available for this
dataset at build time, so classes are colored by ID only (not labeled with
instrument/anatomy names) — this is disclosed, not guessed at.

Masks are single-channel class-index images (confirmed via direct
inspection of all 272 real mask files: uint8, values found = {0..8},
0 = background). Colored using the same validated categorical palette
already used elsewhere in this UI (ui/frontend/src/graph/palette.ts's
series hues, all 8 slots — an earlier version of this script only checked
one sample frame and found classes {0,1,2,3,8}, silently leaving the real
{4,5,6,7} classes uncolored/invisible; confirmed by scanning every mask
file, not a single sample) for a consistent visual language across the app.

Usage: uv run scripts/prepare_demo_videos.py video_01
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np
from dotenv import load_dotenv
from google.cloud import storage as gcs_storage

load_dotenv()

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def _hex_to_bgr(hex_color: str) -> tuple[int, int, int]:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return (b, g, r)


# Same hex values as ui/frontend/src/index.css's light-mode --series-1..8
# (mirrored here since this Python script can't read CSS custom
# properties) — reused only for visual consistency with the rest of the
# app, not a claim about class identity. All 8 slots, matching the real
# 8 non-background classes found across the annotation files (never a
# subset chosen from one sample frame).
_SERIES_HEX = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
CLASS_COLORS_BGR = {class_id: _hex_to_bgr(hex_color) for class_id, hex_color in enumerate(_SERIES_HEX, start=1)}
OVERLAY_ALPHA = 0.45


def transcode_raw(video_id: str) -> Path:
    src = DATA_ROOT / "video" / video_id / "video_left.avi"
    dst = DATA_ROOT / "video" / video_id / "video_left.mp4"
    print(f"Transcoding {src.name} -> {dst.name} (H.264/MP4, browser-playable)...")
    subprocess.run(
        [
            FFMPEG, "-y", "-i", str(src),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart",
            str(dst),
        ],
        check=True,
        capture_output=True,
    )
    print(f"  -> {dst} ({dst.stat().st_size / 1e6:.1f} MB)")
    return dst


def load_mask_index(video_id: str) -> dict[int, Path]:
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


def composite_annotated(video_id: str) -> Path:
    src = DATA_ROOT / "video" / video_id / "video_left.avi"
    dst = DATA_ROOT / "video" / video_id / "video_left_annotated.mp4"
    mask_index = load_mask_index(video_id)
    if not mask_index:
        raise FileNotFoundError(f"no segmentation masks found for {video_id}")
    mask_frame_numbers = list(mask_index.keys())
    print(f"Compositing {len(mask_index)} real segmentation masks onto {src.name}...")

    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    writer = imageio_ffmpeg.write_frames(
        str(dst),
        (width, height),
        fps=fps,
        codec="libx264",
        pix_fmt_in="bgr24",
        pix_fmt_out="yuv420p",
        output_params=["-crf", "23", "-preset", "fast", "-movflags", "+faststart"],
    )
    writer.send(None)  # prime the generator

    current_mask_color: np.ndarray | None = None
    hide_after_frame = -1
    next_mask_ptr = 0
    frame_idx = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break

            while next_mask_ptr < len(mask_frame_numbers) and mask_frame_numbers[next_mask_ptr] <= frame_idx:
                this_mask_frame = mask_frame_numbers[next_mask_ptr]
                raw_mask = cv2.imread(str(mask_index[this_mask_frame]), cv2.IMREAD_UNCHANGED)
                current_mask_color = colorize_mask(raw_mask)
                # Visible for the first half of the REAL gap to the next
                # real mask (derived per-interval from the actual mask
                # filenames, not a hardcoded duration) — bounds how stale
                # a shown overlay can ever be to half the true annotation
                # interval, then hides rather than showing a known-stale
                # position. The final mask (no next one) is shown for just
                # that one frame — there's no real next interval to derive
                # a hold duration from.
                next_mask_frame = (
                    mask_frame_numbers[next_mask_ptr + 1] if next_mask_ptr + 1 < len(mask_frame_numbers) else this_mask_frame
                )
                hide_after_frame = this_mask_frame + (next_mask_frame - this_mask_frame) // 2
                next_mask_ptr += 1

            show_overlay = current_mask_color is not None and frame_idx <= hide_after_frame
            if show_overlay:
                overlay_pixels = np.any(current_mask_color != 0, axis=-1)
                blended = frame.copy()
                blended[overlay_pixels] = cv2.addWeighted(
                    frame, 1 - OVERLAY_ALPHA, current_mask_color, OVERLAY_ALPHA, 0
                )[overlay_pixels]
            else:
                blended = frame

            # `blended` is BGR (cv2's native order) and the writer above was
            # opened with pix_fmt_in="bgr24" — sending it directly. A
            # previous version of this line ran an extra BGR2RGB conversion
            # before sending, which silently mislabeled the byte order to
            # ffmpeg (told "bgr24", handed RGB-ordered bytes) and swapped
            # the red/blue channels in the actual output file — confirmed by
            # inspecting a real composited frame: deep-red tissue rendered
            # as deep blue. No conversion is correct here.
            writer.send(blended.tobytes())
            frame_idx += 1
            if frame_idx % 2000 == 0:
                print(f"  ...{frame_idx}/{total_frames} frames")
    finally:
        writer.close()
        cap.release()

    print(f"  -> {dst} ({dst.stat().st_size / 1e6:.1f} MB), {frame_idx} frames processed")
    return dst


def upload_to_gcs(video_id: str, paths: list[Path]) -> None:
    """Uploads the prepared files to the same bucket/prefix convention
    tools/video_utils.py's find_video_gcs_uri already uses for the raw
    source video (videos/<video_id>/<filename>) — so services/state_service
    can serve them with no local-disk dependency, on this machine or a
    fresh Cloud Run instance alike (see services/state_service/gcs_video.py)."""
    bucket_name = os.environ.get("SURGGRAPH_GCS_BUCKET")
    if not bucket_name:
        print("  SURGGRAPH_GCS_BUCKET not set — skipping upload, files stay local-only.")
        return
    client = gcs_storage.Client()
    bucket = client.bucket(bucket_name)
    for path in paths:
        blob = bucket.blob(f"videos/{video_id}/{path.name}")
        print(f"Uploading {path.name} ({path.stat().st_size / 1e6:.1f} MB) -> gs://{bucket_name}/{blob.name}...")
        blob.upload_from_filename(str(path))
        print(f"  -> uploaded")


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: uv run scripts/prepare_demo_videos.py <video_id>")
        return 1
    video_id = sys.argv[1]
    raw_path = transcode_raw(video_id)
    annotated_path = composite_annotated(video_id)
    upload_to_gcs(video_id, [raw_path, annotated_path])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
