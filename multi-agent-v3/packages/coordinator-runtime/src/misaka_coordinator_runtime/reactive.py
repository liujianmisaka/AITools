from __future__ import annotations

import asyncio

from misaka_coordinator_runtime.contracts import (
    CoordinatorStatus,
    EventEnvelope,
    EventRouteFactory,
    EventSource,
)
from misaka_coordinator_runtime.direct import DirectExecutionHandle
from misaka_coordinator_runtime.errors import CoordinatorStateError


class ReactiveCoordinator:
    """Routes events to independent ExecutionPlans with bounded concurrency."""

    def __init__(
        self,
        source: EventSource,
        route_factory: EventRouteFactory,
        *,
        topic: str | None = None,
        max_concurrency: int = 4,
        shutdown_timeout_seconds: float = 15.0,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._source = source
        self._route_factory = route_factory
        self._topic = topic
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._status = CoordinatorStatus.STOPPED
        self._consumer: asyncio.Task[None] | None = None
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._handles: dict[str, DirectExecutionHandle] = {}
        self._seen_event_ids: set[str] = set()
        self._errors: list[tuple[str, str]] = []
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        self._stopped.set()

    @property
    def status(self) -> CoordinatorStatus:
        return self._status

    @property
    def active_count(self) -> int:
        return len(self._handles)

    @property
    def errors(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._errors)

    async def start(self) -> None:
        async with self._lock:
            if self._status is CoordinatorStatus.ACTIVE:
                return
            if self._status is CoordinatorStatus.STOPPING:
                raise CoordinatorStateError(
                    "coordinator.stopping",
                    "reactive coordinator is stopping",
                )
            self._status = CoordinatorStatus.ACTIVE
            self._stopped.clear()
            self._consumer = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        async with self._lock:
            if self._status is CoordinatorStatus.STOPPED and not self._dispatch_tasks:
                return
            if self._status is CoordinatorStatus.STOPPING:
                wait_for_existing_stop = True
            else:
                self._status = CoordinatorStatus.STOPPING
                wait_for_existing_stop = False
        if wait_for_existing_stop:
            await self._stopped.wait()
            return
        consumer = self._consumer
        if consumer is not None:
            consumer.cancel()
        try:
            async with asyncio.timeout(self._shutdown_timeout_seconds):
                if consumer is not None:
                    await asyncio.gather(consumer, return_exceptions=True)
                handles = tuple(self._handles.values())
                await asyncio.gather(
                    *(handle.cancel("reactive coordinator stopping") for handle in handles),
                    return_exceptions=True,
                )
                if self._dispatch_tasks:
                    await asyncio.gather(*tuple(self._dispatch_tasks), return_exceptions=True)
        except TimeoutError:
            for task in tuple(self._dispatch_tasks):
                task.cancel()
            if self._dispatch_tasks:
                await asyncio.gather(*tuple(self._dispatch_tasks), return_exceptions=True)
        finally:
            self._consumer = None
            self._handles.clear()
            self._status = CoordinatorStatus.STOPPED
            self._stopped.set()

    async def _consume(self) -> None:
        try:
            async for event in self._source.events(topic=self._topic):
                if self._status is not CoordinatorStatus.ACTIVE:
                    return
                async with self._lock:
                    if event.event_id in self._seen_event_ids:
                        continue
                    self._seen_event_ids.add(event.event_id)
                task = asyncio.create_task(self._dispatch(event))
                self._dispatch_tasks.add(task)
                task.add_done_callback(self._dispatch_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._errors.append(("event_source", str(exc)))

    async def _dispatch(self, event: EventEnvelope) -> None:
        async with self._semaphore:
            try:
                plan = await self._route_factory(event)
                if plan is None:
                    return
                handle = DirectExecutionHandle(await plan.start(attempt=1))
                self._handles[event.event_id] = handle
                try:
                    await handle.wait()
                finally:
                    self._handles.pop(event.event_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._errors.append((event.event_id, str(exc)))
