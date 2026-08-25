"""The one genuinely new direct-Firestore read SurgBot needs: a case listing.

No case-listing endpoint or user_id/owner field exists anywhere in the
current system (verified via repo-wide grep this session) — services/
state_service only ever serves ONE case's snapshot/patch stream at a time,
addressed by a case_id the caller already has. SurgBot's whole premise is
"talk about one or more completed cases", which means it needs to discover
what cases even exist first.

Queries ONLY the top-level `cases` collection (`case_id`, `video_id`,
`created_at`, `seq` — the exact shape services/orchestrator_service/main.py
and services/state_service/store.py already write) — never `graph_items`,
which stays exclusively behind the state service's snapshot endpoint. This
module never writes; it is a read-only sibling to that existing write path,
using its own lazy Firestore client (google.cloud.firestore, sync — case
listing is a small, infrequent call, not a latency-critical hot path, so the
plain sync client used read-only here is simpler than standing up a second
async client for one query).

DISCLOSED SIMPLIFICATION (plan §14.1): "cross-case access" means every case
currently in the Firestore project is visible to SurgBot — there is no real
per-user ACL system to build in the time available, and the main pipeline has
none either.

NO FABRICATED DATA. If Firestore has zero cases, list_cases() returns an
empty list. That is the honest, correct result, not a failure to paper over —
this codebase has a standing rule against ever fabricating placeholder case
data on an empty backend result.
"""

from __future__ import annotations

import logging
import os

from google.cloud import firestore
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_client: firestore.Client | None = None


def _database_name() -> str:
    # Same env var, same default, as services/state_service/store.py's
    # _database_name() — SurgBot reads the identical Firestore database, just
    # via its own client and its own (read-only) query, never the same
    # client instance (services/state_service/store.py is off-limits to edit
    # or import from, per the hard constraint on touching existing files).
    return os.environ.get("FIRESTORE_DATABASE", "(default)")


def _get_client() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(database=_database_name())
    return _client


class CaseSummary(BaseModel):
    case_id: str
    video_id: str | None = None
    created_at: str | None = None
    seq: int = 0


def list_cases(limit: int = 200) -> list[CaseSummary]:
    """Lists every case currently in the `cases` collection, newest first.

    Returns [] on a genuinely empty collection — this is a valid, expected
    result (e.g. immediately after a fresh Firestore database is created, or
    before any case has been opened yet), not an error to hide.
    """
    client = _get_client()
    try:
        docs = (
            client.collection("cases")
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        cases = []
        for doc in docs:
            body = doc.to_dict() or {}
            cases.append(
                CaseSummary(
                    case_id=body.get("case_id", doc.id),
                    video_id=body.get("video_id"),
                    created_at=body.get("created_at"),
                    seq=body.get("seq", 0),
                )
            )
        return cases
    except Exception:
        # A case doc written before any graph patch (services/orchestrator_
        # service/main.py's initial write) has no `seq` yet — that's a valid
        # doc shape, not a corruption, but a required-order_by on a field some
        # docs lack can make Firestore's query planner behave oddly on a small
        # collection. Fall back to an unordered scan rather than surfacing a
        # 500 for what is fundamentally still a real, listable set of cases.
        logger.warning("case_index.list_cases: order_by query failed, falling back to unordered scan", exc_info=True)
        docs = client.collection("cases").limit(limit).stream()
        cases = [
            CaseSummary(
                case_id=(doc.to_dict() or {}).get("case_id", doc.id),
                video_id=(doc.to_dict() or {}).get("video_id"),
                created_at=(doc.to_dict() or {}).get("created_at"),
                seq=(doc.to_dict() or {}).get("seq", 0),
            )
            for doc in docs
        ]
        cases.sort(key=lambda c: c.created_at or "", reverse=True)
        return cases
