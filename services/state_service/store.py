"""In-memory Living State Graph store — one process, one dict per case_id.

Hackathon constraints explicitly bless this for the demo ("in-memory graph
is fine"). The original design named GEAP Memory Bank as the intended
durable layer; that integration hasn't been verified yet, so this is the
honest, currently-real implementation — not a stand-in pretending to be
something more durable. Swapping in Memory Bank/Firestore later only
requires reimplementing this module's interface (get_or_create/apply_patch/
snapshot/subscribe/unsubscribe), nothing above it changes.

`seq` is assigned HERE, by the single in-process store, never by a caller —
tools/state_tools.py used to assign it client-side via a local counter,
which is only safe for the local-file fallback (one writer). With a real
server in the loop, this store is the one authority for ordering.
"""

from __future__ import annotations

import asyncio

from state.schema import GraphEdgePatch, GraphNodePatch, StateDiffEvent, StateSnapshot


class CaseGraphState:
    def __init__(self) -> None:
        self.seq: int = 0
        self.nodes: dict[str, GraphNodePatch] = {}
        self.edges: dict[str, GraphEdgePatch] = {}
        self.subscribers: list[asyncio.Queue[StateDiffEvent]] = []


class CaseGraphStore:
    def __init__(self) -> None:
        self._cases: dict[str, CaseGraphState] = {}
        self._lock = asyncio.Lock()

    def _get_or_create(self, case_id: str) -> CaseGraphState:
        return self._cases.setdefault(case_id, CaseGraphState())

    async def snapshot(self, case_id: str) -> StateSnapshot:
        async with self._lock:
            case = self._get_or_create(case_id)
            return StateSnapshot(
                case_id=case_id,
                seq=case.seq,
                nodes=list(case.nodes.values()),
                edges=list(case.edges.values()),
            )

    async def apply_patch(self, case_id: str, incoming: StateDiffEvent) -> StateDiffEvent:
        """Assigns the real seq, applies the patch to this case's graph, and
        broadcasts the finalized event to every active subscriber. Ignores
        whatever seq the caller sent — this store is the sole authority."""
        async with self._lock:
            case = self._get_or_create(case_id)
            case.seq += 1
            finalized = incoming.model_copy(update={"case_id": case_id, "seq": case.seq})

            if finalized.op in ("add_node", "update_node") and finalized.node is not None:
                case.nodes[finalized.node.node_id] = finalized.node
            elif finalized.op in ("add_edge", "update_edge") and finalized.edge is not None:
                case.edges[finalized.edge.edge_id] = finalized.edge
            elif finalized.op == "remove_edge" and finalized.edge is not None:
                case.edges.pop(finalized.edge.edge_id, None)

            subscribers = list(case.subscribers)

        for queue in subscribers:
            await queue.put(finalized)

        return finalized

    async def subscribe(self, case_id: str) -> asyncio.Queue[StateDiffEvent]:
        queue: asyncio.Queue[StateDiffEvent] = asyncio.Queue()
        async with self._lock:
            case = self._get_or_create(case_id)
            case.subscribers.append(queue)
        return queue

    async def unsubscribe(self, case_id: str, queue: asyncio.Queue[StateDiffEvent]) -> None:
        async with self._lock:
            case = self._cases.get(case_id)
            if case is not None and queue in case.subscribers:
                case.subscribers.remove(queue)


store = CaseGraphStore()
