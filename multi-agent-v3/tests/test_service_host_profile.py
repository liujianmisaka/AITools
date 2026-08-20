from __future__ import annotations

import sys

import pytest
from misaka_kernel import HostStatus
from misaka_service_host_profile import ServiceHostConfig, create_service_host
from misaka_service_runtime import ManagedServiceStatus, ServiceDefinition


def _definition(service_id: str) -> ServiceDefinition:
    return ServiceDefinition(
        service_id=service_id,
        display_name=service_id,
        description="test service",
        category="test",
        command=(sys.executable, "-c", "import time; time.sleep(30)"),
        startup_timeout_seconds=1.0,
        shutdown_timeout_seconds=1.0,
    )


@pytest.mark.asyncio
async def test_service_host_profile_exposes_composition_snapshot_and_runtime() -> None:
    profile = create_service_host(
        (_definition("sleep"),),
        config=ServiceHostConfig(profile_id="test-service-host"),
    )
    assert profile.status is HostStatus.CREATED
    assert profile.composition_snapshot is not None
    assert profile.composition_snapshot.profile_id == "test-service-host"
    assert profile.composition_snapshot.fact_owners == (
        ("managed_service.lifecycle", "runtime.managed-service"),
    )
    await profile.start()
    try:
        started = await profile.start_service("sleep")
        assert started.status is ManagedServiceStatus.RUNNING
        assert (await profile.services())[0].epoch == started.epoch
    finally:
        await profile.stop()
    assert profile.status is HostStatus.STOPPED
