"""Quick CLI for reading real errors from the deployed SurgBot root agent —
faster than opening Cloud Console for a quick check. This is how the
QueueFull, resource_exhausted, and httpx.RemoteProtocolError bugs were
actually diagnosed this session (never guessed from the truncated WebSocket
close reason the browser sees).

Usage:
  uv run python3 scripts/tail_surgbot_errors.py                # last 2h, errors only
  uv run python3 scripts/tail_surgbot_errors.py --freshness=30m --severity=WARNING
  uv run python3 scripts/tail_surgbot_errors.py --full          # untruncated tracebacks
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--freshness", default="2h", help="How far back to look (default 2h)")
    parser.add_argument("--severity", default="ERROR", help="Minimum severity (default ERROR)")
    parser.add_argument("--limit", type=int, default=20, help="Max entries (default 20)")
    parser.add_argument("--full", action="store_true", help="Print full untruncated tracebacks")
    args = parser.parse_args()

    query = f'resource.type="aiplatform.googleapis.com/ReasoningEngine" AND severity>={args.severity}'
    cmd = [
        "gcloud", "logging", "read", query,
        f"--project={PROJECT_ID}",
        f"--limit={args.limit}",
        f"--freshness={args.freshness}",
        "--format=json",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return 1

    entries = json.loads(result.stdout or "[]")
    if not entries:
        print(f"No entries at severity>={args.severity} in the last {args.freshness}.")
        return 0

    for entry in entries:
        text = entry.get("textPayload", "")
        ts = entry.get("timestamp", "")
        print(f"=== {ts} [{entry.get('severity')}] ===")
        print(text if args.full else text[:600])
        print()

    print(f"{len(entries)} entries. Console: "
          f"https://console.cloud.google.com/logs/query;query="
          f'resource.type%3D%22aiplatform.googleapis.com%2FReasoningEngine%22%20AND%20severity%3E%3D{args.severity}'
          f"?project={PROJECT_ID}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
