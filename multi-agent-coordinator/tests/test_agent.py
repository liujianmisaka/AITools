import ast
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from agent_framework import Agent, AgentResponse, AgentSession
from agent_framework.exceptions import ChatClientException

from misaka_coordinator_service.application import (
    COORDINATOR_DECISION_RESPONSE_FORMAT,
    CoordinatorAgent,
    CoordinatorAgentConfig,
    CoordinatorAgentError,
    CoordinatorDecisionKind,
    CoordinatorDecisionLimitExceeded,
    CoordinatorDecisionStepError,
    CoordinatorModelUnavailableError,
    CoordinatorReasoningEffort,
    CoordinatorStructuredResponseError,
    dump_agent_session,
    load_agent_session,
)


def decision_value(*, decision_id: str = "decision-1") -> dict[str, object]:
    return {
        "decision_id": decision_id,
        "kind": "delegate",
        "rationale": "需要由 Codex 检查实现",
        "tasks": [
            {
                "task_id": "task-1",
                "objective": "检查实现",
                "acceptance_criteria": ["给出明确结论"],
                "required_capabilities": ["code-review"],
                "constraints": ["禁止 Push"],
                "parent_task_id": None,
            }
        ],
        "selection": {
            "provider_id": "codex",
            "model_id": "pixel/gpt-5.6-luna",
            "effort": "medium",
            "rationale": "适合代码审查",
            "capability_ids": ["code-review"],
        },
        "target_node_id": None,
        "message": None,
    }


def make_config(
    *,
    base_url: str | None = "http://127.0.0.1:10100/v1/",
    max_output_tokens: int = 1200,
    request_timeout_seconds: float = 5.0,
) -> CoordinatorAgentConfig:
    return CoordinatorAgentConfig(
        model="pixel/gpt-5.6-luna",
        api_key="test-token",
        base_url=base_url,
        reasoning_effort=CoordinatorReasoningEffort.MEDIUM,
        max_output_tokens=max_output_tokens,
        max_decision_steps=2,
        request_timeout_seconds=request_timeout_seconds,
    )


def make_agent(
    invoker: Callable[[str, AgentSession], Awaitable[AgentResponse[Any]]],
    *,
    request_timeout_seconds: float = 5.0,
) -> CoordinatorAgent:
    return CoordinatorAgent(
        config=make_config(request_timeout_seconds=request_timeout_seconds),
        invoker=invoker,
    )


def test_openai_factory_builds_real_maf_agent_without_network_access() -> None:
    config = make_config()

    coordinator = CoordinatorAgent.from_openai(config)

    assert isinstance(coordinator.framework_agent, Agent)
    assert cast(object, coordinator.framework_agent.default_options.get("store")) is False
    assert coordinator.request_options == {
        "model": "pixel/gpt-5.6-luna",
        "base_url": "http://127.0.0.1:10100/v1",
        "reasoning_effort": "medium",
        "max_output_tokens": 1200,
        "max_decision_steps": 2,
        "request_timeout_seconds": 5.0,
        "store": False,
    }
    assert "test-token" not in repr(config)
    assert COORDINATOR_DECISION_RESPONSE_FORMAT["type"] == "json_schema"


def test_decide_returns_structured_result_and_persists_step_ledger() -> None:
    prompts: list[str] = []

    async def invoke(prompt: str, session: AgentSession) -> AgentResponse[Any]:
        prompts.append(prompt)
        assert session.session_id == "maf-session-1"
        return AgentResponse[Any](
            value=decision_value(decision_id=f"decision-{len(prompts)}"),
            response_id=f"response-{len(prompts)}",
            finish_reason="stop",
        )

    coordinator = make_agent(invoke)
    session = coordinator.create_session(session_id="maf-session-1")

    first = asyncio.run(
        coordinator.decide(
            "审查当前代码",
            session=session,
            activation_id="activation-1",
            step=1,
        )
    )
    second = asyncio.run(
        coordinator.decide(
            "根据审查结果决定下一步",
            session=session,
            activation_id="activation-1",
            step=2,
        )
    )

    assert first.decision.kind is CoordinatorDecisionKind.DELEGATE
    assert first.decision.tasks[0].task_id == "task-1"
    assert first.decision.selection is not None
    assert first.decision.selection.provider_id == "codex"
    assert first.response_id == "response-1"
    assert first.finish_reason == "stop"
    assert second.decision.decision_id == "decision-2"
    assert '"activation_id":"activation-1"' in prompts[0]
    assert '"decision_step":1' in prompts[0]

    restored = load_agent_session(dump_agent_session(session))
    assert restored.to_dict() == session.to_dict()


def test_decision_steps_are_ordered_and_bounded_per_activation() -> None:
    async def invoke(_prompt: str, _session: AgentSession) -> AgentResponse[Any]:
        return AgentResponse[Any](value=decision_value())

    coordinator = make_agent(invoke)
    session = coordinator.create_session(session_id="maf-session-1")
    asyncio.run(coordinator.decide("first", session=session, activation_id="activation-1", step=1))

    with pytest.raises(CoordinatorDecisionStepError, match="expects step 2"):
        asyncio.run(
            coordinator.decide("repeat", session=session, activation_id="activation-1", step=1)
        )
    with pytest.raises(CoordinatorDecisionStepError, match="new activation"):
        asyncio.run(
            coordinator.decide("skip", session=session, activation_id="activation-2", step=2)
        )
    with pytest.raises(CoordinatorDecisionLimitExceeded, match="max_decision_steps=2"):
        asyncio.run(
            coordinator.decide("overflow", session=session, activation_id="activation-1", step=3)
        )


def test_invalid_structured_response_does_not_advance_step() -> None:
    responses: list[dict[str, object]] = [{"kind": "wait"}, decision_value()]

    async def invoke(_prompt: str, _session: AgentSession) -> AgentResponse[Any]:
        return AgentResponse[Any](value=responses.pop(0))

    coordinator = make_agent(invoke)
    session = coordinator.create_session(session_id="maf-session-1")

    with pytest.raises(CoordinatorStructuredResponseError):
        asyncio.run(
            coordinator.decide("invalid", session=session, activation_id="activation-1", step=1)
        )
    result = asyncio.run(
        coordinator.decide("retry", session=session, activation_id="activation-1", step=1)
    )
    assert result.decision.kind is CoordinatorDecisionKind.DELEGATE


def test_framework_and_timeout_failures_are_normalized() -> None:
    async def fail(_prompt: str, _session: AgentSession) -> AgentResponse[Any]:
        raise ChatClientException("model unavailable")

    async def hang(_prompt: str, _session: AgentSession) -> AgentResponse[Any]:
        await asyncio.sleep(1)
        return AgentResponse[Any](value=decision_value())

    failing = make_agent(fail)
    with pytest.raises(CoordinatorModelUnavailableError, match="request failed"):
        asyncio.run(
            failing.decide(
                "run",
                session=failing.create_session(session_id="session-1"),
                activation_id="activation-1",
                step=1,
            )
        )

    timing_out = make_agent(hang, request_timeout_seconds=0.001)
    with pytest.raises(CoordinatorModelUnavailableError, match="timed out"):
        asyncio.run(
            timing_out.decide(
                "run",
                session=timing_out.create_session(session_id="session-1"),
                activation_id="activation-1",
                step=1,
            )
        )


def test_agent_configuration_and_session_payload_are_strict() -> None:
    with pytest.raises(CoordinatorAgentError, match="HTTP"):
        make_config(base_url="not-a-url")
    with pytest.raises(CoordinatorAgentError, match="max_output_tokens"):
        make_config(max_output_tokens=0)
    with pytest.raises(CoordinatorAgentError, match="valid JSON"):
        load_agent_session("not-json")


def test_application_layer_does_not_import_v3_or_provider_implementations() -> None:
    application_root = (
        Path(__file__).parents[1] / "src" / "misaka_coordinator_service" / "application"
    )
    forbidden_roots = {
        "misaka_control_plane",
        "misaka_invocation_runtime",
        "misaka_codex_provider",
        "misaka_claude_provider",
    }

    imported_roots: set[str] = set()
    for source_path in application_root.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_roots.add(node.module.split(".", maxsplit=1)[0])

    assert imported_roots.isdisjoint(forbidden_roots)
