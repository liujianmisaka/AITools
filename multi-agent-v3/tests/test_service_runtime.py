from __future__ import annotations

import sys

import pytest
from misaka_service_runtime import (
    ManagedServiceStatus,
    ServiceDefinition,
    ServiceManager,
    ServiceManagerError,
)


def _definition(service_id: str, code: str, *, timeout: float = 1.0) -> ServiceDefinition:
    return ServiceDefinition(
        service_id=service_id,
        display_name=service_id,
        description="test service",
        category="test",
        command=(sys.executable, "-c", code),
        startup_timeout_seconds=timeout,
        shutdown_timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_service_manager_starts_lists_and_stops_process_tree() -> None:
    manager = ServiceManager((_definition("sleep", "import time; time.sleep(30)"),))
    await manager.start()
    try:
        started = await manager.start_service("sleep")
        assert started.status is ManagedServiceStatus.RUNNING
        assert started.pid is not None
        listed = await manager.list()
        assert listed[0].service_id == "sleep"
        assert listed[0].pid == started.pid

        stopped = await manager.stop("sleep")
        assert stopped.status is ManagedServiceStatus.STOPPED
        assert stopped.pid is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_service_manager_reports_start_failure_and_can_retry() -> None:
    manager = ServiceManager((_definition("broken", "raise SystemExit(3)", timeout=0.5),))
    await manager.start()
    try:
        with pytest.raises(ServiceManagerError, match="failed to start") as raised:
            await manager.start_service("broken")
        assert raised.value.code == "service.start_failed"
        snapshot = await manager.get("broken")
        assert snapshot.status is ManagedServiceStatus.FAILED
        assert snapshot.pid is None
    finally:
        await manager.close()
