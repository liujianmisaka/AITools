from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from importlib.metadata import version

from agent_framework import (
    AgentSession,
    MCPStdioTool,
    MCPStreamableHTTPTool,
    WorkflowBuilder,
)
from agent_framework.openai import OpenAIChatClient


@dataclass(frozen=True, slots=True)
class BaselineReport:
    agent_framework_core: str
    agent_framework_openai: str
    agent_framework_orchestrations: str
    mcp: str
    session_round_trip: bool
    openai_compatible_client: bool
    mcp_stdio_tool: bool
    mcp_streamable_http_tool: bool
    workflow_builder: bool


def verify_baseline() -> BaselineReport:
    session = AgentSession(session_id="coordinator-baseline")
    restored_session = AgentSession.from_dict(session.to_dict())

    openai_client = OpenAIChatClient(
        model="baseline/model",
        api_key="baseline-token",
        base_url="http://127.0.0.1:10100/v1",
    )
    stdio_tool = MCPStdioTool(
        name="multi-agent-v3",
        command="python",
        args=["-m", "misaka_mcp_gateway"],
        load_tools=False,
        load_prompts=False,
    )
    http_tool = MCPStreamableHTTPTool(
        name="multi-agent-v3-http",
        url="http://127.0.0.1:8016/mcp",
        load_tools=False,
        load_prompts=False,
    )

    return BaselineReport(
        agent_framework_core=version("agent-framework-core"),
        agent_framework_openai=version("agent-framework-openai"),
        agent_framework_orchestrations=version("agent-framework-orchestrations"),
        mcp=version("mcp"),
        session_round_trip=restored_session.session_id == session.session_id,
        openai_compatible_client=(
            openai_client.model == "baseline/model"
            and openai_client.base_url == "http://127.0.0.1:10100/v1"
        ),
        mcp_stdio_tool=stdio_tool.name == "multi-agent-v3",
        mcp_streamable_http_tool=http_tool.name == "multi-agent-v3-http",
        workflow_builder=WorkflowBuilder.__name__ == "WorkflowBuilder",
    )


def main() -> None:
    print(json.dumps(asdict(verify_baseline()), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
