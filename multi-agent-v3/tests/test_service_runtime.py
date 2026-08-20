from __future__ import annotations

import sys

import pytest
from misaka_kernel import Host, HostStatus
from misaka_service_runtime import (
    MANAGED_SERVICE_RUNTIME_SERVICE,
    ManagedServiceRuntimeModule,
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


def _health_definition(service_id: str, url: str) -> ServiceDefinition:
    return ServiceDefinition(
        service_id=service_id,
        display_name=service_id,
        description="test service",
        category="test",
        command=(sys.executable, "-c", "import time; time.sleep(30)"),
        health_url=url,
        startup_timeout_seconds=0.3,
        shutdown_timeout_seconds=0.5,
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


@pytest.mark.asyncio
async def test_service_runtime_module_starts_and_closes_manager_with_host() -> None:
    manager = ServiceManager((_definition("sleep", "import time; time.sleep(30)"),))
    host = Host(name="service-host")
    host.add_module(ManagedServiceRuntimeModule(manager))

    await host.start()
    try:
        assert host.status is HostStatus.ACTIVE
        assert host.services.require(MANAGED_SERVICE_RUNTIME_SERVICE) is manager
        started = await manager.start_service("sleep")
        assert started.epoch == 1
        assert started.process_identity is not None
    finally:
        await host.stop()

    assert host.status is HostStatus.STOPPED
    assert not manager.started


@pytest.mark.asyncio
async def test_service_epoch_fences_stale_stop() -> None:
    manager = ServiceManager((_definition("sleep", "import time; time.sleep(30)"),))
    await manager.start()
    try:
        started = await manager.start_service("sleep")
        with pytest.raises(ServiceManagerError, match="stale"):
            await manager.stop("sleep", expected_epoch=started.epoch - 1)
        stopped = await manager.stop("sleep", expected_epoch=started.epoch)
        assert stopped.status is ManagedServiceStatus.STOPPED
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_service_health_failure_cleans_up_process() -> None:
    manager = ServiceManager((_health_definition("unready", "http://127.0.0.1:1/health"),))
    await manager.start()
    try:
        with pytest.raises(ServiceManagerError, match="failed to start"):
            await manager.start_service("unready")
        snapshot = await manager.get("unready")
        assert snapshot.status is ManagedServiceStatus.FAILED
        assert snapshot.pid is None
        assert snapshot.process_identity is None
    finally:
        await manager.close()


def test_non_controllable_service_is_catalog_only() -> None:
    definition = ServiceDefinition(
        service_id="external",
        display_name="External",
        description="externally managed",
        category="test",
        controllable=False,
    )
    assert definition.command == ()
