"""Thin wrapper on tools/context_slice.py::build_index — the only path SurgBot
uses into existing case data.

tools/context_slice.py::build_index(case_id) -> GraphIndex already does
exactly what's needed: fetches the live snapshot via
tools/state_tools.py::get_state_snapshot() (itself HTTP-to-state-service,
local-JSONL fallback) and indexes it. Nothing here re-derives that — this
module re-exports it plus adds the one genuinely new thing SurgBot needs:
fanning that fetch out concurrently across several cases at once for a
multi-case review session.
"""

from __future__ import annotations

import asyncio

from tools.context_slice import GraphIndex, build_index

__all__ = ["GraphIndex", "build_index", "get_case_indexes"]


async def get_case_indexes(case_ids: list[str]) -> dict[str, GraphIndex]:
    """Fetches and indexes several cases concurrently.

    Returns a dict keyed by case_id rather than a list — SurgBot's tools
    (root_agent.py) always need to look a specific case back up by id, and a
    positional list would silently misalign if any one fetch failed and were
    dropped rather than raised.
    """
    if not case_ids:
        return {}
    indexes = await asyncio.gather(*(build_index(cid) for cid in case_ids))
    return dict(zip(case_ids, indexes))
