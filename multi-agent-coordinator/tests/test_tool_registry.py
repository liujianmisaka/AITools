import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
from agent_framework import FunctionTool

from misaka_coordinator_service.application import CoordinatorAgent, CoordinatorAgentConfig
from misaka_coordinator_service.tools import (
    InMemoryToolAuditSink,
    JsonlToolAuditSink,
    MAFMCPToolSource,
    MCPToolRegistry,
    ToolArgumentsInvalidError,
    ToolDefinition,
    ToolInvocationFailedError,
    ToolInvocationOutcome,
    ToolInvocationTimedOutError,
    ToolNotAvailableError,
    ToolRegistryConfigurationError,
    ToolSourceState,
)

DELEGATE_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "workspace": {"type": "string", "minLength": 1},
        "prompt": {"type": "string", "minLength": 1},
    },
    "required": ["workspace", "prompt"],
}

type InvokeBehavior = Callable[[str, Mapping[str, object]], Awaitable[object]]


async def echo_behavior(tool_name: str, arguments: Mapping[str, object]) -> object:
    return {"tool": tool_name, "workspace": arguments["workspace"]}


class FakeToolSource:
    def __init__(
        self,
        *,
        source_id: str,
        definitions: Sequence[ToolDefinition],
        behavior: InvokeBehavior = echo_behavior,
        discovery_error: Exception | None = None,
    ) -> None:
        self._source_id = source_id
        self._definitions = tuple(definitions)
        self._behavior = behavior
        self._discovery_error = discovery_error
        self.invocations: list[tuple[str, Mapping[str, object]]] = []
        self.closed = False

    @property
    def source_id(self) -> str:
        return self._source_id

    async def discover(self) -> Sequence[ToolDefinition]:
        if self._discovery_error is not None:
            raise self._discovery_error
        return self._definitions

    async def invoke(self, tool_name: str, arguments: Mapping[str, object]) -> object:
        self.invocations.append((tool_name, arguments))
        return await self._behavior(tool_name, arguments)

    async def close(self) -> None:
        self.closed = True


def test_jsonl_tool_audit_persists_only_argument_names(tmp_path: Path) -> None:
    audit_path = tmp_path / "tool-audit.jsonl"
    sink = JsonlToolAuditSink(audit_path)
    registry = MCPToolRegistry(
        sources=(
            FakeToolSource(
                source_id="v3",
                definitions=(
                    ToolDefinition(
                        source_id="v3",
                        name="delegate",
                        description="Delegate work",
                        input_schema=DELEGATE_SCHEMA,
                    ),
                ),
            ),
        ),
        allowed_tool_names=("delegate",),
        audit_sink=sink,
    )

    asyncio.run(registry.refresh())
    asyncio.run(
        registry.invoke(
            "delegate",
            {"workspace": "D:/secret-workspace", "prompt": "sensitive prompt"},
        )
    )

    records = JsonlToolAuditSink(audit_path).records
    assert len(records) == 1
    assert records[0].outcome is ToolInvocationOutcome.SUCCEEDED
    assert records[0].argument_names == ("prompt", "workspace")
    raw = audit_path.read_text(encoding="utf-8")
    assert "D:/secret-workspace" not in raw
    assert "sensitive prompt" not in raw


def definition(
    *,
    source_id: str = "v3",
    name: str = "delegate",
    capabilities: tuple[str, ...] = ("delegation.create",),
) -> ToolDefinition:
    return ToolDefinition(
        source_id=source_id,
        name=name,
        description="Create a V3 delegation",
        input_schema=DELEGATE_SCHEMA,
        capability_ids=capabilities,
    )


def registry_for(
    sources: Sequence[FakeToolSource | MAFMCPToolSource],
    *,
    allowed: Sequence[str] = ("delegate",),
    timeout: float = 1.0,
    audit_sink: InMemoryToolAuditSink | None = None,
) -> tuple[MCPToolRegistry, InMemoryToolAuditSink]:
    sink = audit_sink or InMemoryToolAuditSink()
    registry = MCPToolRegistry(
        sources=sources,
        allowed_tool_names=allowed,
        audit_sink=sink,
        invocation_timeout_seconds=timeout,
        invocation_id_factory=lambda: "invocation-1",
    )
    return registry, sink


def test_refresh_degrades_failed_source_and_classifies_available_tools() -> None:
    good = FakeToolSource(source_id="v3", definitions=(definition(),))
    broken = FakeToolSource(
        source_id="search",
        definitions=(),
        discovery_error=ConnectionError("offline"),
    )
    registry, _sink = registry_for((good, broken), allowed=("delegate", "search"))

    snapshot = asyncio.run(registry.refresh())

    assert snapshot.revision == 1
    assert tuple(tool.name for tool in snapshot.tools) == ("delegate",)
    assert snapshot.sources[0].state is ToolSourceState.AVAILABLE
    assert snapshot.sources[1].state is ToolSourceState.UNAVAILABLE
    assert snapshot.sources[1].error_message == "ConnectionError: offline"
    assert registry.find_by_capability("delegation.create") == (definition(),)
    assert registry.availability("delegate").available
    assert registry.availability("not-allowed").reason == "tool is not in the registry allow-list"
    assert registry.availability("search").reason == (
        "tool was not discovered from an available source"
    )


def test_invoke_validates_arguments_and_records_value_free_audit() -> None:
    source = FakeToolSource(source_id="v3", definitions=(definition(),))
    registry, sink = registry_for((source,))
    asyncio.run(registry.refresh())

    result = asyncio.run(
        registry.invoke("delegate", {"workspace": "D:/dev/project", "prompt": "review"})
    )

    assert result.value == {"tool": "delegate", "workspace": "D:/dev/project"}
    assert result.invocation_id == "invocation-1"
    assert sink.records[-1].outcome is ToolInvocationOutcome.SUCCEEDED
    assert sink.records[-1].argument_names == ("prompt", "workspace")
    assert not hasattr(sink.records[-1], "arguments")

    with pytest.raises(ToolArgumentsInvalidError):
        asyncio.run(registry.invoke("delegate", {"workspace": "D:/dev/project"}))
    assert sink.records[-1].outcome is ToolInvocationOutcome.REJECTED
    assert sink.records[-1].error_code == "arguments_invalid"
    assert len(source.invocations) == 1

    with pytest.raises(ToolNotAvailableError, match="allow-list"):
        asyncio.run(registry.invoke("unknown", {}))
    assert sink.records[-1].error_code == "tool_not_available"


def test_timeout_and_source_failure_are_normalized() -> None:
    async def slow(_tool_name: str, _arguments: Mapping[str, object]) -> object:
        await asyncio.sleep(1)
        return "late"

    async def fail(_tool_name: str, _arguments: Mapping[str, object]) -> object:
        raise OSError("secret endpoint detail")

    slow_source = FakeToolSource(
        source_id="slow-source",
        definitions=(definition(source_id="slow-source", name="slow"),),
        behavior=slow,
    )
    registry, sink = registry_for((slow_source,), allowed=("slow",), timeout=0.001)
    asyncio.run(registry.refresh())
    with pytest.raises(ToolInvocationTimedOutError, match="timed out"):
        asyncio.run(registry.invoke("slow", {"workspace": "w", "prompt": "p"}))
    assert sink.records[-1].outcome is ToolInvocationOutcome.TIMED_OUT

    failing_source = FakeToolSource(
        source_id="failing-source",
        definitions=(definition(source_id="failing-source", name="fail"),),
        behavior=fail,
    )
    registry, sink = registry_for((failing_source,), allowed=("fail",))
    asyncio.run(registry.refresh())
    with pytest.raises(ToolInvocationFailedError, match="tool fail failed") as captured:
        asyncio.run(registry.invoke("fail", {"workspace": "w", "prompt": "p"}))
    assert "secret endpoint detail" not in str(captured.value)
    assert sink.records[-1].outcome is ToolInvocationOutcome.FAILED


def test_duplicate_exposed_names_do_not_replace_previous_snapshot() -> None:
    first = FakeToolSource(source_id="first", definitions=(definition(source_id="first"),))
    second = FakeToolSource(source_id="second", definitions=(definition(source_id="second"),))
    registry, _sink = registry_for((first, second))

    with pytest.raises(ToolRegistryConfigurationError, match="both first and second"):
        asyncio.run(registry.refresh())
    assert registry.snapshot.revision == 0
    assert registry.snapshot.tools == ()


def test_agent_tool_proxy_still_uses_registry_policy_and_audit() -> None:
    source = FakeToolSource(source_id="v3", definitions=(definition(),))
    registry, sink = registry_for((source,))
    asyncio.run(registry.refresh())
    agent_tool = registry.create_agent_tools()[0]

    value = asyncio.run(
        agent_tool.invoke(
            arguments={"workspace": "D:/dev/project", "prompt": "review"},
            skip_parsing=True,
        )
    )

    assert value == {"tool": "delegate", "workspace": "D:/dev/project"}
    assert sink.records[-1].outcome is ToolInvocationOutcome.SUCCEEDED


def test_registry_proxy_tools_attach_to_real_maf_agent() -> None:
    source = FakeToolSource(source_id="v3", definitions=(definition(),))
    registry, _sink = registry_for((source,))
    asyncio.run(registry.refresh())
    coordinator = CoordinatorAgent.from_openai(
        CoordinatorAgentConfig(
            model="pixel/gpt-5.6-luna",
            api_key="test-token",
            base_url="http://127.0.0.1:10100/v1",
        ),
        tools=registry.create_agent_tools(),
    )

    framework_agent = coordinator.framework_agent
    assert framework_agent is not None
    configured_tools = cast(object, framework_agent.default_options.get("tools"))
    assert isinstance(configured_tools, list)
    tools = cast(list[object], configured_tools)
    assert tuple(tool.name for tool in tools if isinstance(tool, FunctionTool)) == ("delegate",)


class FakeMCPClientTool:
    def __init__(self, functions: Sequence[FunctionTool]) -> None:
        self._functions = list(functions)
        self.connected = False
        self.closed = False

    @property
    def functions(self) -> list[FunctionTool]:
        return self._functions

    async def connect(self, *, reset: bool = False) -> None:
        assert not reset
        self.connected = True

    async def close(self) -> None:
        self.closed = True


def test_maf_mcp_source_extracts_schema_capability_and_invokes_function() -> None:
    async def delegate(workspace: str, prompt: str) -> object:
        return {"workspace": workspace, "prompt": prompt}

    function = FunctionTool(
        name="v3_delegate",
        description="Create delegation",
        input_model=DELEGATE_SCHEMA,
        func=delegate,
        additional_properties={"_mcp_remote_name": "delegate"},
    )
    client = FakeMCPClientTool((function,))
    source = MAFMCPToolSource(
        source_id="v3",
        client_tool=client,
        capability_ids_by_tool={"delegate": ("delegation.create",)},
    )

    definitions = asyncio.run(source.discover())
    result = asyncio.run(
        source.invoke("v3_delegate", {"workspace": "D:/dev/project", "prompt": "review"})
    )
    asyncio.run(source.close())

    assert client.connected
    assert client.closed
    assert definitions == (
        ToolDefinition(
            source_id="v3",
            name="v3_delegate",
            description="Create delegation",
            input_schema=DELEGATE_SCHEMA,
            capability_ids=("delegation.create",),
        ),
    )
    assert result == {"workspace": "D:/dev/project", "prompt": "review"}


def test_maf_mcp_source_configuration_rejects_invalid_endpoint() -> None:
    with pytest.raises(ToolRegistryConfigurationError, match="absolute HTTP"):
        MAFMCPToolSource.streamable_http(
            source_id="v3",
            url="not-a-url",
            allowed_tools=("delegate",),
            capability_ids_by_tool={"delegate": ("delegation.create",)},
        )
    with pytest.raises(ToolRegistryConfigurationError, match="greater than zero"):
        MAFMCPToolSource.stdio(
            source_id="v3",
            command="python",
            args=("-m", "misaka_mcp_gateway"),
            allowed_tools=("delegate",),
            capability_ids_by_tool={"delegate": ("delegation.create",)},
            request_timeout_seconds=0,
        )
