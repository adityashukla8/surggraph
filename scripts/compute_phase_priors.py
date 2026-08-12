"""Computes empirical phase(action)-transition priors from SAR-RARP50's own
`action_continuous.txt` files — the dataset's real, already run-length-encoded
per-video action timeline (format: `start_frame,end_frame,action_id`).

No phase taxonomy or transition table is authored here. The set of action
IDs and their transition statistics are entirely counted from whatever
`data/annotations/<video_id>/action_continuous.txt` files are present —
add more downloaded videos and rerun; nothing else changes. See
tools/phase_transition_priors.py::get_phase_transition_priors for the
runtime tool that reads this file's output, and plan §2 for why this
replaces a hand-authored state machine.

FPS is read from each video file's real metadata (cv2), never assumed.

Usage: uv run scripts/compute_phase_priors.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, stdev

from tools.video_utils import find_video_fps

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
ANNOTATIONS_DIR = DATA_ROOT / "annotations"
OUT_PATH = DATA_ROOT / "priors" / "phase_transition_matrix.json"


class Segment:
    __slots__ = ("start_frame", "end_frame", "action_id")

    def __init__(self, start_frame: int, end_frame: int, action_id: int):
        self.start_frame = start_frame
        self.end_frame = end_frame
        self.action_id = action_id

    @property
    def duration_frames(self) -> int:
        return self.end_frame - self.start_frame


def parse_action_continuous(path: Path) -> list[Segment]:
    segments = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        start_s, end_s, action_s = line.split(",")
        segments.append(Segment(int(start_s), int(end_s), int(action_s)))
    return segments


def main() -> int:
    video_dirs = sorted(p for p in ANNOTATIONS_DIR.iterdir() if p.is_dir() and (p / "action_continuous.txt").exists())
    if not video_dirs:
        print(f"[FAILED] No action_continuous.txt found under {ANNOTATIONS_DIR} — nothing to compute.")
        return 1

    transition_counts: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    duration_frames_by_action: dict[int, list[int]] = defaultdict(list)
    source_videos: list[str] = []
    fps_by_video: dict[str, float] = {}

    for video_dir in video_dirs:
        video_id = video_dir.name
        segments = parse_action_continuous(video_dir / "action_continuous.txt")
        if not segments:
            print(f"[SKIP] {video_id}: action_continuous.txt is empty")
            continue

        fps = find_video_fps(video_id)
        if fps is None:
            print(f"[WARN] {video_id}: could not read FPS from data/video/{video_id}/ — duration stats will be frame-only")
        else:
            fps_by_video[video_id] = fps

        source_videos.append(video_id)
        for seg in segments:
            duration_frames_by_action[seg.action_id].append(seg.duration_frames)
        for a, b in zip(segments, segments[1:]):
            transition_counts[a.action_id][b.action_id] += 1

        print(f"[OK] {video_id}: {len(segments)} segments, fps={fps}")

    if not source_videos:
        print("[FAILED] No usable video directories found.")
        return 1

    phases = sorted(set(transition_counts.keys()) | {a for d in transition_counts.values() for a in d} | set(duration_frames_by_action.keys()))
    # A single fps is used for duration-in-seconds conversion — fine while
    # every downloaded video is 60fps (verified per-video above); if a
    # future video has a different fps, per-action duration stats should be
    # computed in seconds per-video before aggregating, not mixed as frames.
    fps_values = set(fps_by_video.values())
    if len(fps_values) > 1:
        print(f"[WARN] Multiple distinct FPS values across videos {fps_values} — duration_stats.mean_s below mixes frame rates, treat with caution.")
    representative_fps = next(iter(fps_values), None)

    transition_probs: dict[str, dict[str, float]] = {}
    for src, dests in transition_counts.items():
        total = sum(dests.values())
        transition_probs[str(src)] = {str(dst): count / total for dst, count in dests.items()}

    duration_stats: dict[str, dict] = {}
    for action_id, durations in duration_frames_by_action.items():
        stats = {
            "n": len(durations),
            "mean_frames": mean(durations),
            "median_frames": median(durations),
            "std_frames": stdev(durations) if len(durations) > 1 else 0.0,
        }
        if representative_fps:
            stats["mean_s"] = stats["mean_frames"] / representative_fps
            stats["median_s"] = stats["median_frames"] / representative_fps
            stats["std_s"] = stats["std_frames"] / representative_fps
        duration_stats[str(action_id)] = stats

    output = {
        "source_videos": source_videos,
        "fps_by_video": fps_by_video,
        "phases": [str(p) for p in phases],
        "transition_counts": {str(k): {str(k2): v2 for k2, v2 in v.items()} for k, v in transition_counts.items()},
        "transition_probs": transition_probs,
        "duration_stats": duration_stats,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator_script": "scripts/compute_phase_priors.py",
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote priors for {len(phases)} action classes across {len(source_videos)} video(s) to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
