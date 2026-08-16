from __future__ import annotations

import asyncio

from multi_agent_v2.packages.observability.health import (
    ComponentStatus,
    HealthService,
    ReadinessStatus,
)


class PassingProbe:
    name = "passing"

    async def check(self) -> None:
        return None


class FailingProbe:
    name = "failing"

    async def check(self) -> None:
        raise ConnectionError("must not be returned by the public health endpoint")


class SlowProbe:
    name = "slow"

    async def check(self) -> None:
        await asyncio.sleep(1)


async def test_health_service_reports_ready_when_all_components_are_up() -> None:
    report = await HealthService([PassingProbe()], timeout_seconds=0.1).report()

    assert report.status is ReadinessStatus.READY
    assert report.components[0].status is ComponentStatus.UP


async def test_health_service_reports_sanitized_dependency_failure() -> None:
    report = await HealthService([FailingProbe()], timeout_seconds=0.1).report()

    assert report.status is ReadinessStatus.NOT_READY
    assert report.components[0].status is ComponentStatus.DOWN
    assert report.components[0].detail == "ConnectionError"
    assert "must not be returned" not in report.model_dump_json()


async def test_health_service_bounds_each_probe_with_a_timeout() -> None:
    report = await HealthService([SlowProbe()], timeout_seconds=0.01).report()

    assert report.status is ReadinessStatus.NOT_READY
    assert report.components[0].detail == "TimeoutError"
