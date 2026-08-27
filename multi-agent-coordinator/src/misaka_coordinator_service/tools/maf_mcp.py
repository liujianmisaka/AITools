from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast
from urllib.parse import urlparse

from agent_framework import FunctionTool, MCPStdioTool, MCPStreamableHTTPTool

from misaka_coordinator_service.domain._serialization import ensure_text
from misaka_coordinator_service.tools.registry import (
    ToolDefinition,
    ToolNotAvailableError,
    ToolRegistryConfigurationError,
)


class MAFMCPClientTool(Protocol):
    @property
    def functions(self) -> list[FunctionTool]: ...

    async def connect(self, *, reset: bool = False) -> None: ...

    async def close(self) -> None: ...


class MAFMCPToolSource:
    def __init__(
        self,
        *,
        source_id: str,
        client_tool: MAFMCPClientTool,
        capability_ids_by_tool: Mapping[str, Sequence[str]],
    ) -> None:
        self._source_id = ensure_text(source_id, "source_id")
        self._client_tool = client_tool
        self._capability_ids_by_tool = {
            ensure_text(name, "tool_name"): tuple(
                ensure_text(capability_id, "capability_id") for capability_id in capability_ids
            )
            for name, capability_ids in capability_ids_by_tool.items()
        }
        self._functions: dict[str, FunctionTool] = {}

    @classmethod
    def streamable_http(
        cls,
        *,
        source_id: str,
        url: str,
        allowed_tools: Sequence[str],
        capability_ids_by_tool: Mapping[str, Sequence[str]],
        request_timeout_seconds: int = 30,
        tool_name_prefix: str | None = None,
    ) -> MAFMCPToolSource:
        if request_timeout_seconds < 1:
            raise ToolRegistryConfigurationError(
                "request_timeout_seconds must be greater than zero"
            )
        normalized_url = ensure_text(url, "url").rstrip("/")
        parsed = urlparse(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ToolRegistryConfigurationError("MCP url must be an absolute HTTP(S) URL")
        client_tool = MCPStreamableHTTPTool(
            name=source_id,
            url=normalized_url,
            allowed_tools=tuple(ensure_text(name, "allowed_tools") for name in allowed_tools),
            tool_name_prefix=tool_name_prefix,
            load_prompts=False,
            request_timeout=request_timeout_seconds,
        )
        return cls(
            source_id=source_id,
            client_tool=client_tool,
            capability_ids_by_tool=capability_ids_by_tool,
        )

    @classmethod
    def stdio(
        cls,
        *,
        source_id: str,
        command: str,
        args: Sequence[str],
        allowed_tools: Sequence[str],
        capability_ids_by_tool: Mapping[str, Sequence[str]],
        env: Mapping[str, str] | None = None,
        request_timeout_seconds: int = 30,
        tool_name_prefix: str | None = None,
    ) -> MAFMCPToolSource:
        if request_timeout_seconds < 1:
            raise ToolRegistryConfigurationError(
                "request_timeout_seconds must be greater than zero"
            )
        client_tool = MCPStdioTool(
            name=source_id,
            command=ensure_text(command, "command"),
            args=[ensure_text(arg, "args") for arg in args],
            env=None if env is None else dict(env),
            allowed_tools=tuple(ensure_text(name, "allowed_tools") for name in allowed_tools),
            tool_name_prefix=tool_name_prefix,
            load_prompts=False,
            request_timeout=request_timeout_seconds,
        )
        return cls(
            source_id=source_id,
            client_tool=client_tool,
            capability_ids_by_tool=capability_ids_by_tool,
        )

    @property
    def source_id(self) -> str:
        return self._source_id

    async def discover(self) -> Sequence[ToolDefinition]:
        await self._client_tool.connect()
        functions: dict[str, FunctionTool] = {}
        definitions: list[ToolDefinition] = []
        for function in self._client_tool.functions:
            schema = self._read_input_schema(function)
            remote_name = self._read_remote_name(function)
            capability_ids = self._capability_ids_by_tool.get(
                function.name,
                self._capability_ids_by_tool.get(remote_name, ()),
            )
            functions[function.name] = function
            definitions.append(
                ToolDefinition(
                    source_id=self._source_id,
                    name=function.name,
                    description=function.description,
                    input_schema=schema,
                    capability_ids=tuple(capability_ids),
                )
            )
        self._functions = functions
        return tuple(definitions)

    async def invoke(self, tool_name: str, arguments: Mapping[str, object]) -> object:
        function = self._functions.get(tool_name)
        if function is None:
            raise ToolNotAvailableError(f"MCP source {self._source_id} has no tool {tool_name}")
        value = await function.invoke(
            arguments=cast(Mapping[str, Any], arguments),
            skip_parsing=True,
        )
        return cast(object, value)

    async def close(self) -> None:
        await self._client_tool.close()

    @staticmethod
    def _read_remote_name(function: FunctionTool) -> str:
        properties = function.additional_properties or {}
        remote_name = cast(object, properties.get("_mcp_remote_name"))
        return remote_name if isinstance(remote_name, str) else function.name

    @staticmethod
    def _read_input_schema(function: FunctionTool) -> Mapping[str, object]:
        value = cast(object, function.to_json_schema_spec())
        if not isinstance(value, dict):
            raise ValueError(f"tool {function.name} schema must be an object")
        root = cast(dict[object, object], value)
        function_value = root.get("function")
        if not isinstance(function_value, dict):
            raise ValueError(f"tool {function.name} schema has no function object")
        function_object = cast(dict[object, object], function_value)
        parameters = function_object.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError(f"tool {function.name} schema has no parameters object")
        raw_parameters = cast(dict[object, object], parameters)
        if any(not isinstance(key, str) for key in raw_parameters):
            raise ValueError(f"tool {function.name} schema parameter keys must be strings")
        return cast(dict[str, object], raw_parameters)
