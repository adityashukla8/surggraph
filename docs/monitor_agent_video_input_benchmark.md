# Monitor Agent: native video vs. extracted still frames — benchmark and migration decision

**Date:** 2026-08-13
**Status:** Decided — migrating to native video input for `agents/monitor/`'s sub-agents.

## Background

The Monitor Agent's three sub-agents (Temporal, Spatial, Procedural — see `agents/monitor/subagents.py`) were originally built to receive their per-window visual input as a set of individually extracted, JPEG-encoded still frames (`tools/video_utils.py::sample_frames` — `cv2.VideoCapture` seeks to N evenly-spaced frame numbers within a window, reads the raw BGR array, resizes, and encodes each to JPEG, sent as separate `types.Part` image objects in one multimodal `Content`).

The question: does Gemini's native video-input capability (clip directly from a Cloud Storage-hosted video via `start_offset`/`end_offset`/`fps`, no local frame extraction) do better on cost, accuracy, or both?

## Technical mechanism (confirmed against the installed `google-genai` SDK, not assumed from docs)

`google.genai.types.Part` has a `video_metadata` field. `types.VideoMetadata` has exactly three fields:

| Field | Type | Notes |
|---|---|---|
| `start_offset` | `str` | e.g. `"229s"` — clips the video to begin here |
| `end_offset` | `str` | e.g. `"239s"` — clips the video to end here |
| `fps` | `float` | valid range `(0.0, 24.0]`, default `1.0` if unset |

Usage (confirmed working, `vertexai=True`, our exact setup):

```python
types.Content(
    role="user",
    parts=[
        types.Part.from_text(text=instruction),
        types.Part(
            file_data=types.FileData(file_uri="gs://bucket/path/video.avi", mime_type="video/x-msvideo"),
            video_metadata=types.VideoMetadata(fps=5.0, start_offset="229s", end_offset="239s"),
        ),
    ],
)
```

**Hard constraint:** `Part.from_bytes` (inline video bytes) caps at 100MB. Our video (`video_left.avi`) is 483.9MB, so inline bytes are not an option — the video must be referenced via a GCS `file_uri`. This lines up with the architecture's already-planned event flow (video lands in GCS first), just not previously exercised for this video during Monitor Agent dev/testing, which had been working off the local file only.

**Action taken:** uploaded the video once to `gs://surggraph-cases-liveapi-488810/videos/video_01/video_left.avi` (507,436,052 bytes, matches the local file exactly). Every future call references this same URI with different `start_offset`/`end_offset`/`fps` — no re-upload per call, per window, or per agent.

## Benchmark methodology

Same window, same instruction, same task, two input methods, directly compared:

- **Window:** `video_01-w0229` — frames 13740–14328, i.e. **229.0s–239.0s** in the source video (60fps, 271.4s total). This is a real, previously-verified ground-truth-positive window (confirmed via `tools/sedmamba_labels.py::window_ground_truth` before this benchmark, never shown to the model).
- **Instruction:** the Temporal agent's real pass-1 screen instruction (`agents/monitor/subagents.py::_screen_instruction("temporal")`), unmodified between the two runs — full 6-category CARES knowledge block embedded, identical `output_schema=ScreenOutput`.
- **Model:** `gemini-3.5-flash` via Vertex AI, `global` location (our standard `GlobalGemini` setup, `tools/gemini_model.py`).
- **Method A — native video:** one `Part` with `file_data` (the GCS URI above) + `video_metadata=VideoMetadata(fps=5.0, start_offset="229s", end_offset="239s")`. `fps=5.0` chosen per a published Gemini temporal-fidelity benchmark showing accuracy improves from ~27% (1 FPS) to ~38% (5 FPS) on motion-sensitive tasks, then plateaus or regresses at 8–16 FPS.
- **Method B — still frames (pre-migration approach):** `tools/video_utils.py::sample_frames` extracting 10 frames evenly spaced across the same frame range (13740–14328), resized to 960×540, JPEG quality 85 (the Temporal agent's original `FRAME_SAMPLING_PROFILE`), sent as 10 separate image `Part`s with per-frame captions.

Both calls used the real Gemini API (`client.aio.models.generate_content`) directly (not routed through the ADK `Runner`, to get direct access to `response.usage_metadata` for exact token accounting).

## Results (exact, unedited)

| Metric | Native video (fps=5.0) | Still frames (10 @ 960×540) |
|---|---|---|
| `prompt_token_count` | **4,562** | **12,423** |
| — video/image modality tokens | 3,300 (VIDEO) | 11,000 (IMAGE) |
| — text modality tokens | 1,262 | 1,423 |
| `candidates_token_count` | 108 | 186 |
| `thoughts_token_count` | 1,010 | 2,048 |
| `total_token_count` | **5,680** | **14,657** |
| Wall-clock latency | 56.7s | 19.8s |
| Model output | `out_of_view` suspected, confidence 0.95 — *"The active needle driver leaves the field of view completely between 03:53.800 and 03:55.000 while the needle remains embedded in the tissue"* | `multiple_attempts` suspected (0.85) and `needle_handling` suspected (0.85); **no opinion returned on `out_of_view`** |

## Interpretation

1. **Cost: ~2.7× fewer prompt tokens for native video** (4,562 vs. 12,423). Per-frame cost differs structurally: native video tokenizes at a flat rate (3,300 tokens ÷ 50 frame-equivalents at 5fps×10s = **66 tokens/frame**), while our full-resolution-adjacent still images get split into multiple tiles by Gemini's image tiling and cost far more per frame (11,000 tokens ÷ 10 frames = **1,100 tokens/frame**, ~17× more per frame than a native video frame at this sampling density).

2. **A real accuracy difference, not noise.** The two methods reached *different* conclusions on the *identical* window. Native video caught a brief (~1.2s) instrument-out-of-view moment that the still-frame method's 10 evenly-spaced samples apparently fell between and missed — a concrete instance of denser/native temporal sampling catching a brief event that sparser discrete sampling can miss by chance. This is not a controlled multi-trial study (n=1 comparison), but it's a real, non-cherry-picked instance from a genuine ground-truth-positive window.

3. **Timestamps are more useful.** Native video's answer cited an absolute source-video timestamp (`03:53.800`–`03:55.000`, i.e. 233.8s–235.0s, correctly within the 229–239s clipped window) rather than our still-frame captions' frame-number-only references (`"Frame 3/10: video frame #13860"`).

4. **Real cost: ~2.9× higher latency** (56.7s vs. 19.8s) — plausibly GCS fetch + video decode overhead on Gemini's serving side. This matters more for the live-demo-segment path (plan §3.5's bounded `start_s`/`end_s` window during an actual recording) than for the offline validation sweep, where wall-clock time was already known to be the binding constraint (see the sweep's own ~33s/window average finding) and total token cost matters more than any single call's latency.

## Open question, not yet tested

Whether `Part.media_resolution` (a separate field, applicable to images) meaningfully sharpens native video frames the way requesting full resolution currently does for the Spatial agent's still-frame calls. Not blocking the migration decision — the Spatial agent's `fps` can be set low (few frame-equivalents per window) to approximate its current "few frames, high detail" intent, and `media_resolution` can be revisited later if Spatial-agent accuracy looks weak in the post-migration validation sweep.

## Decision

Migrate `agents/monitor/`'s sub-agent frame pipeline from locally-extracted still frames to native video (`file_uri` + `VideoMetadata`), given the real, verified cost and accuracy findings above. Each role keeps a differentiated sampling density via `fps` (replacing the old per-role `n_frames`/`resize_to`/`jpeg_quality` profile):

| Role | Old profile | New profile |
|---|---|---|
| Temporal | 10 frames, 960×540, q85 | `fps=5.0` (dense — matches the benchmarked accuracy sweet spot) |
| Spatial | 4 frames, native res, q95 | `fps=0.4` (~4 frame-equivalents over a 10s window — few, but see open question above re: resolution) |
| Procedural | 6 frames, native res, q90 | `fps=0.6` (~6 frame-equivalents — middle ground) |

`tools/video_utils.py`'s still-frame extraction functions (`sample_frames`, `frames_to_gemini_parts`, `build_multimodal_content`) are kept, not deleted — they may still be useful for other agents (e.g. a future Scene Graph Builder) or for UI-facing thumbnail generation; only the Monitor Agent's LLM-input path switches to native video.
