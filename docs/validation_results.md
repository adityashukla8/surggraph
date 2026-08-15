# Validation results — tracked over time

Consolidated, dated record of every real validation run against this project — the single place to check "how good is this actually," instead of digging through code comments or re-running things to find out. **Update this file every time a validation script gets run for real**, not just when something changes; a missing entry here is a real gap, not an implied "unchanged."

Before this file existed, results were scattered across `agents/monitor/aggregation.py`'s own TODO comment and `docs/latency_optimization.md` (which is about latency, not accuracy) — kept there too where relevant, but this is now the canonical index.

## Monitor Agent — macro-F1 vs. CARES' published 54.3

| Date | Command | n windows | macro_f1 | vs. CARES (0.543) | Confusion (tp/fp/fn/tn) | Notes |
|---|---|---|---|---|---|---|
| 2026-08-13 | `run_monitor_validation_sweep.py video_01` (default: 262 windows, stride=1s) | 262 | **0.408** | -0.135 | 119/18/111/13 | Pre frame-driven restructuring. Archived: `data/validation/monitor_accuracy_2026-08-13_pre_framedriven.jsonl` |
| 2026-08-15 | `run_monitor_validation_sweep.py video_01 --stride-s 5` | 53 | **0.515** | -0.028 | 29/2/18/4 | Post frame-driven restructuring (both tiers stills, unconditional deep, 5s live window_s — this sweep itself still uses window_s=10 default, only stride widened). Coarser sample than the 08-13 run (53 vs 262 windows) — directional, not a strict re-test. Current log: `data/validation/monitor_accuracy.jsonl` |

**Live pattern, unchanged across both runs:** confusion is heavily skewed toward false negatives relative to false positives, meaning `DEFAULT_THRESHOLD=1.7` (`agents/monitor/aggregation.py`) is likely too conservative for this video's real data. Never re-tuned — real, actionable, still open.

**To reproduce:** `uv run scripts/run_monitor_validation_sweep.py video_01 [--stride-s N] [--start-s S --end-s E]`, then `uv run scripts/summarize_monitor_accuracy.py`. Full default sweep (stride=1) is a real ~2.5 hour job at current per-call latency (~53 windows took 762s real wall time) — check with whoever's running it before defaulting to the full sweep.

## Anticipation Agent — change-point accuracy & self-consistency

No ground-truth name legend exists for this dataset, so "was the phase NAME right" isn't a scoreable question (by design — see `agents/anticipation/agent.py`'s own docstring). Two honest, real proxies instead:

| Date | Records | Change-point (precision/recall/f1) | Self-consistency | Notes |
|---|---|---|---|---|
| 2026-08-15 | 189 (accumulated across the day's live test runs, several different case_ids) | precision=0.79 recall=0.67 f1=0.72 (tp=92 fp=24 fn=46 tn=17) | 33.5% overall (4,182 same-real-phase pairs compared; per-phase range 6.7%-40.0%) | First real run of the fixed summarize script — `scripts/summarize_anticipation_accuracy.py` was broken (crashed with `KeyError: 'correct'`) until today; it was written for an earlier log schema and never updated after the final Anticipation redesign. Fixed to match the current schema and the two metrics the agent's own docstring always said should be used. |

**Reading these honestly:** change-point recall=0.67 means the agent misses roughly a third of real transitions (describes the phase as unchanged when it actually changed) — a real, disclosed gap, not tuned yet. Self-consistency at 33.5% is genuinely low, but this run mixes many different real live-test sweeps with different window boundaries and case conditions from across one working session, not one clean controlled experiment — worth a dedicated single-sweep run before treating this number as a stable baseline.

**To reproduce:** the log (`data/validation/anticipation_accuracy.jsonl`) accumulates automatically every time `anticipate_case` runs live (via Orchestrator or a direct test) — nothing to trigger separately. Then `uv run scripts/summarize_anticipation_accuracy.py`.

## Test suite

| Date | Command | Result |
|---|---|---|
| 2026-08-15 | `uv run pytest tests/ -q` | 45/45 passing |

Real-data assertions throughout (no mocked-Gemini harness by design — matches `test_fhir_write_readback.py`'s own "hits the real thing" philosophy). Does not cover live Gemini vision calls themselves; those are only exercised by real end-to-end runs, not unit tests.

## Frontend

No automated test suite yet. `npx tsc --noEmit` (from `ui/frontend/`) for typechecking only — last run clean 2026-08-15.
