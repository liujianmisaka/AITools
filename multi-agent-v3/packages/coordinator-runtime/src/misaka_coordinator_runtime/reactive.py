from __future__ import annotations

import asyncio

from misaka_coordinator_runtime.contracts import (
    CoordinatorStatus,
    EventDeliveryStatus,
    EventDeliveryStore,
    EventEnvelope,
    EventRouteFactory,
    EventSource,
    ExecutionStatus,
)
from misaka_coordinator_runtime.delivery import MemoryEventDeliveryStore
from misaka_coordinator_runtime.direct import DirectExecutionHandle
from misaka_coordinator_runtime.errors import CoordinatorStateError
from misaka_coordinator_runtime.start import start_execution


class ReactiveCoordinator:
    """Routes events to independent executions with durable at-least-once delivery."""

    def __init__(
        self,
        source: EventSource,
        route_factory: EventRouteFactory,
        *,
        topic: str | None = None,
        max_concurrency: int = 4,
        shutdown_timeout_seconds: float = 15.0,
        delivery_store: EventDeliveryStore | None = None,
        consumer_id: str = "reactive-coordinator",
        max_delivery_attempts: int = 1,
        start_sequence: int | None = None,
        close_source: bool = False,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        if not consumer_id.strip():
            raise ValueError("consumer_id must not be empty")
        if max_delivery_attempts < 1:
            raise ValueError("max_delivery_attempts must be at least one")
        if start_sequence is not None and start_sequence < 1:
            raise ValueError("start_sequence must be at least one when provided")
        self._source = source
        self._route_factory = route_factory
        self._topic = topic
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._delivery_store = delivery_store or MemoryEventDeliveryStore()
        self._consumer_id = consumer_id
        self._max_delivery_attempts = max_delivery_attempts
        self._start_sequence = start_sequence
        self._close_source = close_source
        self._status = CoordinatorStatus.STOPPED
        self._consumer: asyncio.Task[None] | None = None
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._handles: dict[str, DirectExecutionHandle] = {}
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
            cursor = await self._delivery_store.cursor(self._consumer_id)
            self._status = CoordinatorStatus.ACTIVE
            self._stopped.clear()
            self._consumer = asyncio.create_task(
                self._consume(max(cursor + 1, self._start_sequence or 1))
            )

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
            if self._close_source:
                await self._source.close()
            self._consumer = None
            self._handles.clear()
            self._status = CoordinatorStatus.STOPPED
            self._stopped.set()

    async def _consume(self, start_sequence: int) -> None:
        try:
            async for event in self._source.events(
                start_sequence=start_sequence,
                topic=self._topic,
            ):
                if self._status is not CoordinatorStatus.ACTIVE:
                    return
                task = asyncio.create_task(self._dispatch(event))
                self._dispatch_tasks.add(task)
                task.add_done_callback(self._dispatch_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._errors.append(("event_source", str(exc)))

    async def _dispatch(self, event: EventEnvelope) -> None:
        async with self._semaphore:
            record = await self._delivery_store.claim(
                self._consumer_id,
                event.event_id,
                event.sequence,
            )
            if record.status is not EventDeliveryStatus.RUNNING:
                return
            handle: DirectExecutionHandle | None = None
            last_error: str | None = None
            try:
                for attempt in range(record.attempts, self._max_delivery_attempts + 1):
                    try:
                        plan = await self._route_factory(event)
                        if plan is None:
                            await self._delivery_store.complete(
                                record,
                                status=EventDeliveryStatus.SUCCEEDED,
                            )
                            return
                        handle = DirectExecutionHandle(
                            await start_execution(
                                plan,
                                attempt=attempt,
                                cancellation_reason="reactive dispatch cancelled during start",
                            )
                        )
                        self._handles[event.event_id] = handle
                        result = await handle.wait()
                        if result.status is ExecutionStatus.RECONCILIATION_REQUIRED:
                            await self._delivery_store.complete(
                                record,
                                status=EventDeliveryStatus.RECONCILIATION_REQUIRED,
                                error_message=result.error_message,
                            )
                            return
                        if result.status is ExecutionStatus.SUCCEEDED:
                            await self._delivery_store.complete(
                                record,
                                status=EventDeliveryStatus.SUCCEEDED,
                            )
                            return
                        last_error = result.error_message or result.status.value
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        last_error = str(exc) or exc.__class__.__name__
                    finally:
                        self._handles.pop(event.event_id, None)
                await self._delivery_store.complete(
                    record,
                    status=EventDeliveryStatus.FAILED,
                    error_message=last_error or "event delivery retry limit exceeded",
                )
                if last_error is not None:
                    self._errors.append((event.event_id, last_error))
            except asyncio.CancelledError:
                if handle is not None:
                    try:
                        await handle.cancel("reactive dispatch cancelled")
                    except Exception:
                        pass
                raise
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                self._errors.append((event.event_id, message))
