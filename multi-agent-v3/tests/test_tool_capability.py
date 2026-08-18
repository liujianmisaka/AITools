from __future__ import annotations

import asyncio
from typing import cast

import pytest
from misaka_kernel import ProfileDefinition, ProfileLoader
from misaka_kernel_contracts import JsonObject
from misaka_tool_capability import (
    MEMORY_TOOL_MODULE_ID,
    TOOL_CAPABILITY_ID,
    TOOL_PROVIDER_SERVICE,
    MemoryToolModule,
    MemoryToolProvider,
    ToolDescriptor,
    ToolInvocation,
    ToolStatus,
)


def _add_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        tool_id="math.add",
        display_name="Add numbers",
        description="Add two integers",
        input_schema={
            "type": "object",
            "required": ["left", "right"],
            "properties": {
                "left": {"type": "integer"},
                "right": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        output_schema={"type": "integer"},
    )


@pytest.mark.asyncio
async def test_memory_tool_provider_discovers_executes_and_deduplicates() -> None:
    calls = 0

    async def add(arguments: JsonObject) -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return cast(int, arguments["left"]) + cast(int, arguments["right"])

    provider = MemoryToolProvider()
    provider.register("math.add", _add_descriptor(), add)
    descriptor = await provider.describe()
    assert descriptor.capability_id == TOOL_CAPABILITY_ID
    assert [tool.tool_id for tool in await provider.tools()] == ["math.add"]

    request = ToolInvocation("tool-inv-1", "math.add", {"left": 2, "right": 3}, "key-1")
    first = await provider.execute(request)
    second = await provider.execute(request)
    assert first.status is ToolStatus.SUCCEEDED
    assert first.output == 5
    assert second == first
    assert calls == 1
    await provider.close()


@pytest.mark.asyncio
async def test_memory_tool_provider_merges_concurrent_idempotent_calls() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_add(arguments: JsonObject) -> int:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 2

    provider = MemoryToolProvider()
    provider.register("math.add", _add_descriptor(), slow_add)
    first_request = ToolInvocation("tool-inv-1", "math.add", {"left": 1, "right": 1}, "key-1")
    second_request = ToolInvocation("tool-inv-2", "math.add", {"left": 1, "right": 1}, "key-1")
    first = asyncio.create_task(provider.execute(first_request))
    await asyncio.wait_for(started.wait(), timeout=1)
    second = asyncio.create_task(provider.execute(second_request))
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result.status is ToolStatus.SUCCEEDED
    assert second_result == first_result
    await provider.close()


@pytest.mark.asyncio
async def test_memory_tool_provider_rejects_schema_and_idempotency_conflicts() -> None:
    provider = MemoryToolProvider()
    provider.register("math.add", _add_descriptor(), lambda arguments: 1)
    invalid = await provider.execute(ToolInvocation("tool-inv-1", "math.add", {"left": 2}, "key-1"))
    assert invalid.status is ToolStatus.REJECTED
    assert invalid.error_code == "tool.input_contract_violated"
    conflict = await provider.execute(
        ToolInvocation("tool-inv-2", "math.add", {"left": 2, "right": 4}, "key-1")
    )
    assert conflict.error_code == "tool.idempotency_conflict"
    await provider.close()


@pytest.mark.asyncio
async def test_memory_tool_provider_rejects_invalid_output() -> None:
    provider = MemoryToolProvider()
    provider.register("math.add", _add_descriptor(), lambda arguments: "not an integer")
    result = await provider.execute(
        ToolInvocation("tool-inv-1", "math.add", {"left": 2, "right": 3}, "key-1")
    )
    assert result.status is ToolStatus.FAILED
    assert result.error_code == "tool.output_contract_violated"
    await provider.close()


@pytest.mark.asyncio
async def test_memory_tool_provider_cancels_running_handler() -> None:
    started = asyncio.Event()

    async def wait_forever(arguments: JsonObject) -> str:
        del arguments
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    provider = MemoryToolProvider()
    provider.register(
        "test.wait",
        ToolDescriptor(
            tool_id="test.wait",
            display_name="Wait",
            description="Wait until cancelled",
            input_schema={"type": "object"},
        ),
        wait_forever,
    )
    request = ToolInvocation("tool-inv-1", "test.wait", {}, "key-1")
    task = asyncio.create_task(provider.execute(request))
    await asyncio.wait_for(started.wait(), timeout=1)
    await provider.cancel(request.invocation_id, "test cancellation")
    result = await asyncio.wait_for(task, timeout=1)
    assert result.status is ToolStatus.CANCELLED
    await provider.close()


@pytest.mark.asyncio
async def test_memory_tool_module_binds_provider_to_kernel() -> None:
    provider = MemoryToolProvider()
    module = MemoryToolModule(provider)
    loader = ProfileLoader({MEMORY_TOOL_MODULE_ID: lambda: module})
    host = loader.create_host(
        ProfileDefinition(
            profile_id="tool-test",
            module_ids=(MEMORY_TOOL_MODULE_ID,),
        )
    )
    await host.start()
    try:
        assert host.services.require(TOOL_PROVIDER_SERVICE) is provider
    finally:
        await host.stop()
