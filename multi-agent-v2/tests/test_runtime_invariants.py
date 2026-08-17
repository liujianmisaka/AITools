from __future__ import annotations

import pytest

from multi_agent_v2.packages.runtime_invariants import (
    InvariantRegistry,
    InvariantViolation,
    assert_monotonic_sequence,
    assert_single_terminal,
    assert_terminal_transition,
)


def test_builtin_invariants_reject_invalid_execution_relations() -> None:
    with pytest.raises(InvariantViolation, match="contiguous"):
        assert_monotonic_sequence("events", [1, 3])
    with pytest.raises(InvariantViolation, match="multiple terminal"):
        assert_single_terminal("execution", ["running", "failed", "succeeded"])
    with pytest.raises(InvariantViolation, match="terminal state"):
        assert_terminal_transition("execution", "succeeded", "running")


@pytest.mark.asyncio
async def test_registry_selection_and_disposal_are_deterministic() -> None:
    calls: list[str] = []
    registry = InvariantRegistry(allowlist=(r"^agent\.",), blocklist=(r"\.slow$",))

    async def selected() -> None:
        calls.append("selected")

    async def blocked() -> None:
        calls.append("blocked")

    dispose = registry.register("agent.execution", selected)
    registry.register("agent.slow", blocked)
    registry.register("control.projection", blocked)

    await registry.check_all()
    dispose()
    await registry.check_all()

    assert calls == ["selected"]


@pytest.mark.asyncio
async def test_registry_attributes_failures_to_the_owner() -> None:
    registry = InvariantRegistry()

    async def broken() -> None:
        raise RuntimeError("bad relation")

    registry.register("agent.execution", broken)

    with pytest.raises(InvariantViolation, match=r"agent\.execution.*bad relation"):
        await registry.check_all()
