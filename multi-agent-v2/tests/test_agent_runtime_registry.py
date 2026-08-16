from __future__ import annotations

import pytest

from multi_agent_v2.packages.agent_runtime import (
    AgentRuntime,
    AgentRuntimeError,
    AgentRuntimeRegistry,
    FakeRuntime,
)


class OtherFakeRuntime(FakeRuntime):
    name = "other"
    runtime_id = "fake:other:v1"
    catalog_revision = "other-catalog-v1"


class BrokenDescriptionRuntime(OtherFakeRuntime):
    name = "broken"

    async def describe(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        raise AgentRuntimeError("catalog failed", code="agent.catalog_unavailable")


def test_fake_runtime_satisfies_runtime_protocol() -> None:
    assert isinstance(FakeRuntime(), AgentRuntime)


def test_registry_rejects_duplicate_and_unknown_runtime() -> None:
    registry = AgentRuntimeRegistry([FakeRuntime()])

    with pytest.raises(AgentRuntimeError) as duplicate:
        registry.register(FakeRuntime())
    assert duplicate.value.code == "agent.runtime_duplicate"

    with pytest.raises(AgentRuntimeError) as missing:
        registry.get("missing")
    assert missing.value.code == "agent.runtime_not_found"


@pytest.mark.asyncio
async def test_registry_describes_in_sorted_isolated_results() -> None:
    registry = AgentRuntimeRegistry([OtherFakeRuntime(), BrokenDescriptionRuntime(), FakeRuntime()])

    descriptions = await registry.describe()

    assert [item.name for item in descriptions] == ["broken", "fake", "other"]
    assert descriptions[0].available is False
    assert descriptions[0].error is not None
    assert descriptions[0].error.code == "agent.catalog_unavailable"
    assert descriptions[1].available is True
    assert descriptions[2].available is True


@pytest.mark.asyncio
async def test_registry_lists_models_and_closes_once() -> None:
    fake = FakeRuntime()
    other = OtherFakeRuntime()
    registry = AgentRuntimeRegistry([fake, other])

    catalog = await registry.list_models("fake", refresh=True)
    await registry.aclose()
    await registry.aclose()

    assert [model.id for model in catalog.models] == ["fake/model"]
    assert fake.close_count == 1
    assert other.close_count == 1
    with pytest.raises(AgentRuntimeError) as captured:
        registry.register(OtherFakeRuntime())
    assert captured.value.code == "agent.registry_closed"
