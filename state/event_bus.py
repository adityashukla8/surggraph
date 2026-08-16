"""In-process graph-change bus — docs/agentic_workflow.md §7.

Event-driven agents must not poll the graph: polling wastes calls and misses
fast events. Instead they subscribe here, and every successful write through
`tools/state_tools.py::apply_state_patch` publishes to the case's bus.

WHY IN-PROCESS RATHER THAN A FIRESTORE LISTENER
Latency (sub-millisecond vs. hundreds of ms through Firestore's Listen
backend) and dependency simplification. Firestore stays the durable store and
the source of the SSE stream to the frontend; this bus is a sibling subscriber
to the same write path, not a second consumer of Firestore's listener contract.

THE INVARIANT THIS DEPENDS ON, stated plainly because it is easy to break
later: all of a case's agents run inside the one process that owns that case
(the orchestrator's background task). The bus taps the CALLER side of
apply_state_patch — inside orchestrator_service, where agents actually execute
— not the store side inside state_service, which is a separate process. If a
case's agents were ever split across processes or instances, this bus would
silently miss cross-process writes. One case = one orchestrator task is what
makes it correct.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from state.schema import StateDiffEvent

logger = logging.getLogger(__name__)

Handler = Callable[[StateDiffEvent], Awaitable[None]]
Predicate = Callable[[StateDiffEvent], bool]


@dataclass
class Subscription:
    name: str
    handler: Handler
    node_types: frozenset[str] | None = None
    predicate: Predicate | None = None

    def matches(self, event: StateDiffEvent) -> bool:
        if self.node_types is not None:
            if event.node is None or event.node.node_type not in self.node_types:
                return False
        if self.predicate is not None and not self.predicate(event):
            return False
        return True


@dataclass
class CaseEventBus:
    """One bus per case. Isolation is by construction — a subscription on one
    case can never see another case's writes, matching the per-case Firestore
    partitioning the store already guarantees."""

    case_id: str
    _subscriptions: list[Subscription] = field(default_factory=list)
    _tasks: set[asyncio.Task] = field(default_factory=set)
    # (subscription name, triggering node/edge id) pairs already dispatched.
    # docs §9: "the event bus may occasionally deliver duplicates" — agents
    # each carry their own idempotency key, but suppressing an obvious repeat
    # here saves a redundant Gemini call rather than relying on every agent to
    # catch it after the fact.
    _delivered: set[tuple[str, str]] = field(default_factory=set)
    _closed: bool = False

    def subscribe(
        self,
        name: str,
        handler: Handler,
        *,
        node_types: set[str] | None = None,
        predicate: Predicate | None = None,
    ) -> None:
        self._subscriptions.append(
            Subscription(
                name=name,
                handler=handler,
                node_types=frozenset(node_types) if node_types else None,
                predicate=predicate,
            )
        )
        logger.info("event_bus[%s]: %s subscribed (node_types=%s)", self.case_id, name, sorted(node_types or []))

    def publish(self, event: StateDiffEvent) -> None:
        """Dispatch matching handlers as independent tasks.

        Deliberately NOT async and never awaited by the writer: a write must
        not block on whatever reasoning it happens to trigger, or the fast
        perception loop would stall behind a multi-second complication call.
        """
        if self._closed:
            return

        key_id = event.node.node_id if event.node else (event.edge.edge_id if event.edge else event.event_id)

        for sub in self._subscriptions:
            if not sub.matches(event):
                continue
            dedupe_key = (sub.name, key_id)
            if dedupe_key in self._delivered:
                logger.debug("event_bus[%s]: %s already handled %s, skipping", self.case_id, sub.name, key_id)
                continue
            self._delivered.add(dedupe_key)
            self._spawn(sub, event)

    def _spawn(self, sub: Subscription, event: StateDiffEvent) -> None:
        task = asyncio.create_task(self._run_handler(sub, event), name=f"{self.case_id}:{sub.name}")
        # Held in a set until done: asyncio only keeps a weak reference to a
        # running task, so a task nobody holds can be garbage-collected
        # mid-flight and simply vanish.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_handler(self, sub: Subscription, event: StateDiffEvent) -> None:
        try:
            await sub.handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failing downstream agent must never take down the writer or its
            # siblings. docs §10: every degradation stays visible rather than
            # being swallowed — hence a real exception log, not a silent pass.
            logger.exception("event_bus[%s]: handler %s failed on event %s", self.case_id, sub.name, event.event_id)

    async def drain(self, timeout_s: float = 30.0) -> None:
        """Wait for in-flight handlers, then cancel whatever is still running.

        Called at case close (docs §6 step 5). Bounded on purpose: a hung
        handler must not hold the case open indefinitely, and the post-case
        agents need to start.
        """
        self._closed = True
        if not self._tasks:
            return
        pending = list(self._tasks)
        logger.info("event_bus[%s]: draining %d in-flight handler(s)", self.case_id, len(pending))
        done, still_pending = await asyncio.wait(pending, timeout=timeout_s)
        if still_pending:
            logger.warning(
                "event_bus[%s]: %d handler(s) exceeded the %.0fs drain budget, cancelling",
                self.case_id,
                len(still_pending),
                timeout_s,
            )
            for task in still_pending:
                task.cancel()
            await asyncio.gather(*still_pending, return_exceptions=True)

    def cancel_all(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()

    @property
    def in_flight(self) -> int:
        return len(self._tasks)


# --- Per-case registry ------------------------------------------------------
# apply_state_patch has only a case_id to work with at the call site, so the
# bus has to be reachable by that alone.

_buses: dict[str, CaseEventBus] = {}


def get_bus(case_id: str) -> CaseEventBus:
    """Gets or creates the case's bus."""
    bus = _buses.get(case_id)
    if bus is None:
        bus = CaseEventBus(case_id=case_id)
        _buses[case_id] = bus
    return bus


def bus_for(case_id: str) -> CaseEventBus | None:
    """The bus if one exists, without creating it. Used by the write path so a
    process with no subscribers (a standalone script, a test) does not
    accumulate a bus per case_id it happens to write to."""
    return _buses.get(case_id)


def publish(case_id: str, event: StateDiffEvent) -> None:
    """Publish a committed write. Called by apply_state_patch AFTER the write
    succeeds, so a handler reading the graph always sees the state that
    triggered it. No-op when nothing subscribes."""
    bus = _buses.get(case_id)
    if bus is not None:
        bus.publish(event)


async def close_bus(case_id: str, timeout_s: float = 30.0) -> None:
    """Drain and discard a case's bus at close."""
    bus = _buses.pop(case_id, None)
    if bus is not None:
        await bus.drain(timeout_s=timeout_s)
