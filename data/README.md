# Data — manual download required

UCL RDR (hosts both datasets below) sits behind an AWS WAF JavaScript
challenge that blocks scripted downloads (`curl`/`wget`/headless HTTP all
get stuck at `202` with an empty body — confirmed 2026-08-12). Download
these through a real browser.

Per `initial_11082026.md` §4.2/§8: **download exactly one SAR-RARP50 video**
end-to-end (with its matching annotations + SEDMamba error labels) — not
the full 50-video corpus.

## 1. SAR-RARP50 video + action/segmentation annotations

Project page: https://rdr.ucl.ac.uk/projects/SAR-RARP50_Segmentation_of_surgical_instrumentation_and_Action_Recognition_on_Robot-Assisted_Radical_Prostatectomy_Challenge/191091

Pick **one** `video_*` item from the project's file listing and download it.
Per the SAR-RARP50 evaluation repo, each video's directory contains:
- the video file itself
- `action_discrete.txt` — per-frame action labels (this is the real,
  dataset-native taxonomy the Anticipation Agent's transition priors get
  computed from — see `scripts/compute_phase_priors.py`, written once we
  can see the real file)
- `segmentation/` — PNG instrument segmentation masks per frame

Place everything for that one video under:
```
data/video/<video_id>/          # the video file
data/annotations/<video_id>/    # action_discrete.txt + segmentation/
```

## 2. SEDMamba error annotations

DOI: https://doi.org/10.5522/04/27992702 (redirects to the RDR article page)

Download `error_annotation_SAR-RARP50.zip` and unzip it. Per the SEDMamba
repo, it contains one `.pkl` file per video with `error_GT` (binary
normal/error per frame at 5Hz), `feature` (DINOv2 embeddings — not needed
here), and `image_name`. **Only keep the `.pkl` for the same `<video_id>`
you downloaded above** — no need for all 48.

Place it at:
```
data/annotations/<video_id>/error_annotation.pkl
```

## After downloading

Run `uv run scripts/validate_downloaded_data.py <video_id>` — it reports
what it finds (file presence, action label count, error label count,
whether the categorical 24-error-type breakdown is present or only the
binary signal) so the rest of the build can proceed against the real
format instead of an assumed one.
