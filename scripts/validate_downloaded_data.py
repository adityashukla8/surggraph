"""Reports what's actually present in a manually-downloaded SAR-RARP50 +
SEDMamba video directory, so the rest of the build (compute_phase_priors.py,
the SEDMamba loader, the Anticipation Agent) can be written against the real
file format rather than an assumed one.

Deliberately reports findings honestly rather than assuming success — every
check either confirms what it found or says plainly what's missing/unusual,
per data/README.md's placement convention:
  data/video/<video_id>/
  data/annotations/<video_id>/{action_discrete.txt, segmentation/, error_annotation.pkl}

Usage: uv run scripts/validate_downloaded_data.py <video_id>
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"


def check_video(video_id: str) -> None:
    video_dir = DATA_ROOT / "video" / video_id
    if not video_dir.exists():
        print(f"[MISSING] {video_dir} does not exist")
        return
    files = sorted(p for p in video_dir.iterdir() if p.is_file())
    if not files:
        print(f"[EMPTY] {video_dir} exists but has no files")
        return
    for f in files:
        size_mb = f.stat().st_size / (1024 * 1024)
        print(f"[FOUND] video file: {f.name} ({size_mb:.1f} MB)")


def check_action_labels(video_id: str) -> None:
    path = DATA_ROOT / "annotations" / video_id / "action_discrete.txt"
    if not path.exists():
        print(f"[MISSING] {path}")
        return
    lines = path.read_text().splitlines()
    print(f"[FOUND] action_discrete.txt: {len(lines)} lines (format: frame,action_id)")
    action_ids = sorted({int(line.split(",")[1]) for line in lines if line.strip()})
    print(f"        unique action_ids: {action_ids}")
    frame_numbers = [int(line.split(",")[0]) for line in lines if line.strip()]
    if len(frame_numbers) > 1:
        step = frame_numbers[1] - frame_numbers[0]
        print(f"        frame sampling step: {step} (frame 0 to {frame_numbers[-1]})")


def check_action_continuous(video_id: str) -> None:
    path = DATA_ROOT / "annotations" / video_id / "action_continuous.txt"
    if not path.exists():
        print(f"[MISSING] {path}")
        return
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    print(f"[FOUND] action_continuous.txt: {len(lines)} segments (format: start_frame,end_frame,action_id)")
    print("        already run-length encoded by the dataset — compute_phase_priors.py")
    print("        can count start->end transitions directly, no segmentation logic needed.")
    for line in lines[:5]:
        print(f"        {line}")
    if len(lines) > 5:
        print(f"        ... ({len(lines) - 5} more)")


def check_segmentation(video_id: str) -> None:
    seg_dir = DATA_ROOT / "annotations" / video_id / "segmentation"
    if not seg_dir.exists():
        print(f"[MISSING] {seg_dir}")
        return
    pngs = list(seg_dir.glob("*.png"))
    print(f"[FOUND] segmentation/: {len(pngs)} PNG files")


def check_error_annotation(video_id: str) -> None:
    path = DATA_ROOT / "annotations" / video_id / "error_annotation.pkl"
    if not path.exists():
        print(f"[MISSING] {path}")
        return
    with open(path, "rb") as f:
        obj = pickle.load(f)
    print(f"[FOUND] error_annotation.pkl: top-level type {type(obj)}")
    if isinstance(obj, dict):
        print(f"        keys: {list(obj.keys())}")
        for key in ("error_GT", "image_name", "feature"):
            if key in obj:
                val = obj[key]
                length = len(val) if hasattr(val, "__len__") else "?"
                print(f"        obj['{key}']: type={type(val)} len={length}")
        if "error_GT" in obj:
            gt = obj["error_GT"]
            unique_vals = set(int(v) for v in gt) if hasattr(gt, "__iter__") else {gt}
            print(f"        error_GT unique values: {sorted(unique_vals)}")
            if unique_vals - {0, 1}:
                print("        NOTE: values beyond {0,1} found — categorical error-type breakdown may be present")
            else:
                print("        NOTE: binary-only signal confirmed (no categorical error-type field in this dict)")


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: uv run scripts/validate_downloaded_data.py <video_id>")
        return 1
    video_id = sys.argv[1]
    print(f"=== Validating data for {video_id} ===\n")
    check_video(video_id)
    print()
    check_action_labels(video_id)
    print()
    check_action_continuous(video_id)
    print()
    check_segmentation(video_id)
    print()
    check_error_annotation(video_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
