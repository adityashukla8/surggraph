"""Live view of what SurgGraph has actually written to the FHIR server.

    uv run scripts/fhir_inbox.py            # snapshot
    uv run scripts/fhir_inbox.py --watch    # refresh until interrupted
    uv run scripts/fhir_inbox.py --case case-abc123

Reads from the real server, never from our own graph. That distinction is the
whole point: our UI showing an alert only proves we recorded an intention, and
this proves the record exists on somebody else's system and can be found there
by anyone with the URL.

Queries by our identifier SYSTEM (`urn:surggraph:divergence-alert`), so it
returns exactly the alerts this project wrote and nothing else from a public
sandbox that anyone can post to.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

FHIR_BASE_URL = os.environ.get("FHIR_BASE_URL", "https://hapi.fhir.org/baseR4")
IDENTIFIER_SYSTEM = "urn:surggraph:divergence-alert"

_PRIORITY_MARK = {"stat": "!!!", "urgent": " !!", "routine": "  ."}


def fetch(case_id: str | None, limit: int) -> list[dict]:
    params = {
        # A bare `system|` matches any value in that system — every alert this
        # project has written, and nothing else on a shared public sandbox.
        "identifier": f"{IDENTIFIER_SYSTEM}|",
        "_sort": "-_lastUpdated",
        "_count": limit,
    }
    resp = requests.get(f"{FHIR_BASE_URL}/Communication", params=params, timeout=30)
    resp.raise_for_status()
    entries = [e["resource"] for e in resp.json().get("entry", [])]
    if case_id:
        entries = [r for r in entries if r.get("identifier", [{}])[0].get("value", "").startswith(f"{case_id}:")]
    return entries


def render(resources: list[dict]) -> None:
    print(f"FHIR inbox — {FHIR_BASE_URL}/Communication  (identifier system: {IDENTIFIER_SYSTEM})")
    print("=" * 100)
    if not resources:
        print("\n  No alerts on the server yet.")
        print("  This is the correct state when the verification gate has blocked everything it saw —")
        print("  an ungrounded complication is refused rather than alerted on.\n")
        return

    for r in resources:
        priority = r.get("priority", "routine")
        sent = (r.get("meta", {}).get("lastUpdated") or r.get("sent") or "")[:19].replace("T", " ")
        print(f"\n{_PRIORITY_MARK.get(priority, '   ')} [{priority.upper():<7}] {sent}   Communication/{r['id']}")
        print(f"      {FHIR_BASE_URL}/Communication/{r['id']}")
        print(f"      case: {r.get('identifier', [{}])[0].get('value', '')}")
        for payload in r.get("payload", []):
            text = payload.get("contentString", "")
            head, *rest = text.split(" — ", 1)
            print(f"        {head}")
            if rest:
                print(f"          {rest[0][:88]}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="refresh until interrupted")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--case", help="only this case_id")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    if not args.watch:
        render(fetch(args.case, args.limit))
        return 0

    seen: set[str] = set()
    try:
        while True:
            resources = fetch(args.case, args.limit)
            print("\033[2J\033[H", end="")  # clear, so the view is the current state
            render(resources)
            new = {r["id"] for r in resources} - seen
            if new and seen:
                print(f"  ** {len(new)} NEW alert(s) since last refresh **\n")
            seen |= {r["id"] for r in resources}
            print(f"  refreshing every {args.interval:.0f}s — Ctrl-C to stop")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
