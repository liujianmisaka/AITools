from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from multi_agent_v2.packages.agent_runtime import (
    AgentExecutionIdentity,
    AgentPolicyContext,
    AgentReconcileRequest,
    AgentResumeRequest,
    AgentStartRequest,
    WorkspaceLease,
    validate_agent_stream,
)
from multi_agent_v2.packages.agent_runtime.codex import CodexRuntime
from multi_agent_v2.packages.agent_runtime.codex_locator import CodexRuntimeLocator
from multi_agent_v2.packages.agent_runtime.errors import AgentRuntimeError
from multi_agent_v2.packages.workflow_dsl.ir import StrictSchemaIr


class _Turn:
    id = "turn-1"

    def __init__(self) -> None:
        self.steer_calls: list[str] = []
        self.interrupted = False

    async def stream(self):
        yield SimpleNamespace(
            type="item/agentMessage/delta",
            payload={"delta": '{"result":'},
        )
        yield SimpleNamespace(
            type="item/completed",
            payload={
                "turnId": self.id,
                "item": {
                    "id": "message-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": '{"result":3}',
                },
            },
        )
        yield SimpleNamespace(
            type="turn/completed",
            payload={
                "turn": {"id": self.id, "status": "completed", "items": []},
            },
        )

    async def steer(self, message: str) -> None:
        self.steer_calls.append(message)

    async def interrupt(self) -> None:
        self.interrupted = True


class _InterruptedTurn(_Turn):
    async def stream(self):
        yield SimpleNamespace(
            type="turn/completed",
            payload={
                "turn": {"id": self.id, "status": "interrupted", "items": []},
            },
        )


class _FailedTurn(_Turn):
    async def stream(self):
        yield SimpleNamespace(
            type="turn/completed",
            payload={
                "turn": {
                    "id": self.id,
                    "status": "failed",
                    "items": [],
                    "error": {
                        "message": ("exceeded retry limit, last status: 429 Too Many Requests"),
                        "codex_error_info": {
                            "response_too_many_failed_attempts": {"http_status_code": 429}
                        },
                    },
                }
            },
        )


class _IncompleteTurn(_Turn):
    async def stream(self):
        if False:
            yield None


class _Thread:
    id = "thread-1"

    def __init__(self, turn: _Turn) -> None:
        self.native_turn = turn
        self.turn_calls: list[tuple[str, dict[str, object]]] = []

    async def turn(self, prompt: str, **kwargs: object) -> _Turn:
        self.turn_calls.append((prompt, kwargs))
        return self.native_turn


class _Client:
    def __init__(self, thread: _Thread) -> None:
        self.thread = thread
        self.entered = False
        self.closed = False
        self.thread_start_calls: list[dict[str, object]] = []
        self.thread_resume_calls: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    async def models(self, *, include_hidden: bool):
        assert include_hidden is False
        return {
            "data": [
                {
                    "id": "sensenova/deepseek-v4-flash",
                    "displayName": "DeepSeek V4 Flash",
                    "supportedReasoningEfforts": ["high", "ultra"],
                }
            ]
        }

    async def thread_start(self, **kwargs: object) -> _Thread:
        self.thread_start_calls.append(kwargs)
        return self.thread

    async def thread_resume(self, session_id: str, **kwargs: object) -> _Thread:
        self.thread_resume_calls.append((session_id, kwargs))
        return self.thread


class _Sdk:
    class Sandbox:
        read_only = "read_only"
        workspace_write = "workspace_write"

    class ApprovalMode:
        deny_all = "deny_all"
        auto_review = "auto_review"

    def __init__(self, turn: _Turn | None = None) -> None:
        self.turn = turn or _Turn()
        self.thread = _Thread(self.turn)
        self.clients: list[_Client] = []
        self.configs: list[dict[str, object]] = []

    def CodexConfig(self, **kwargs: object) -> dict[str, object]:
        self.configs.append(kwargs)
        return kwargs

    def AsyncCodex(self, _config: object) -> _Client:
        client = _Client(self.thread)
        self.clients.append(client)
        return client


def _schema() -> StrictSchemaIr:
    canonical = json.dumps(
        {
            "type": "object",
            "properties": {"result": {"type": "integer"}},
            "required": ["result"],
            "additionalProperties": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return StrictSchemaIr(
        canonical=canonical,
        sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def _request(workspace: Path) -> AgentStartRequest:
    return AgentStartRequest(
        identity=AgentExecutionIdentity(
            execution_id="workflow-1:extract:1",
            workflow_instance_id="workflow-1",
            node_id="extract",
            activation=1,
            attempt=1,
            idempotency_key="workflow-1:extract:1",
        ),
        provider="codex",
        model="sensenova/deepseek-v4-flash",
        effort="high",
        workspace=WorkspaceLease(
            lease_id="workspace-lease-1",
            workspace_id="repo",
            root=workspace,
            access_mode="read_only",
            isolated=False,
        ),
        prompt="Calculate the input.",
        resolved_inputs={"formula": "1 + 2"},
        output_schema=_schema(),
        timeout_ms=60_000,
        policy=AgentPolicyContext(
            sandbox_mode="read_only",
            network_policy="agent_default",
        ),
    )


def _runtime(
    tmp_path: Path,
    sdk: _Sdk,
    *,
    network_deny_is_enforced: bool = False,
) -> CodexRuntime:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model_provider = "sensenova"\n[model_providers.sensenova]\nname = "SenseNova"\n',
        encoding="utf-8",
    )
    return CodexRuntime(
        sdk_module=sdk,
        runtime_locator=CodexRuntimeLocator(
            codex_home=codex_home,
            environ={},
            user_home=tmp_path,
        ),
        catalog_ttl_seconds=60,
        network_deny_is_enforced=network_deny_is_enforced,
    )


async def test_codex_prepares_session_before_starting_turn(tmp_path: Path) -> None:
    sdk = _Sdk()
    runtime = _runtime(tmp_path, sdk)
    request = _request(tmp_path)

    session = await runtime.prepare_session(request)

    assert session.provider_session_id == "thread-1"
    assert sdk.thread.turn_calls == []
    execution_client = sdk.clients[-1]
    assert execution_client.thread_start_calls[0]["model"] == request.model

    handle = await runtime.start_turn(request, session)
    events = [
        event
        async for event in validate_agent_stream(
            runtime.stream(handle),
            execution_id=request.identity.execution_id,
            provider_session_id=handle.provider_session_id,
        )
    ]

    assert sdk.thread.turn_calls[0][1]["model"] == request.model
    assert sdk.thread.turn_calls[0][1]["effort"] == request.effort
    assert events[-1].kind == "completed"
    assert events[-1].output == {"result": 3}
    assert execution_client.closed is True


async def test_codex_resume_is_explicit_and_lost_process_is_uncertain(tmp_path: Path) -> None:
    sdk = _Sdk()
    runtime = _runtime(tmp_path, sdk)
    start = _request(tmp_path)
    resume = AgentResumeRequest(
        **start.model_dump(exclude={"session_mode"}),
        provider_session_id="existing-thread",
    )

    session = await runtime.prepare_session(resume)

    assert session.provider_session_id == "thread-1"
    assert sdk.clients[-1].thread_resume_calls[0][0] == "existing-thread"
    lost = await CodexRuntime(
        sdk_module=sdk,
        runtime_locator=runtime._locator,  # pyright: ignore[reportPrivateUsage]
    ).reconcile(
        AgentReconcileRequest(
            execution_id=resume.identity.execution_id,
            provider_session_id=session.provider_session_id,
            phase="running",
        )
    )
    assert lost.status == "uncertain"


async def test_codex_cancel_waits_for_provider_terminal_confirmation(tmp_path: Path) -> None:
    sdk = _Sdk(_InterruptedTurn())
    runtime = _runtime(tmp_path, sdk)
    request = _request(tmp_path)
    session = await runtime.prepare_session(request)
    handle = await runtime.start_turn(request, session)

    cancel = await runtime.cancel(handle)
    before_confirmation = await runtime.reconcile(
        AgentReconcileRequest(execution_id=request.identity.execution_id)
    )
    events = [
        event
        async for event in validate_agent_stream(
            runtime.stream(handle),
            execution_id=request.identity.execution_id,
            provider_session_id=handle.provider_session_id,
        )
    ]
    after_confirmation = await runtime.reconcile(
        AgentReconcileRequest(execution_id=request.identity.execution_id)
    )

    assert cancel.status == "requested"
    assert sdk.turn.interrupted is True
    assert before_confirmation.status == "running"
    assert events[-1].kind == "cancelled"
    assert after_confirmation.status == "cancelled"


async def test_codex_preserves_provider_failure_message(tmp_path: Path) -> None:
    sdk = _Sdk(_FailedTurn())
    runtime = _runtime(tmp_path, sdk)
    request = _request(tmp_path)
    session = await runtime.prepare_session(request)
    handle = await runtime.start_turn(request, session)

    events = [
        event
        async for event in validate_agent_stream(
            runtime.stream(handle),
            execution_id=request.identity.execution_id,
            provider_session_id=handle.provider_session_id,
        )
    ]
    reconciled = await runtime.reconcile(
        AgentReconcileRequest(execution_id=request.identity.execution_id)
    )

    assert events[-1].kind == "failed"
    assert events[-1].error is not None
    assert events[-1].error.message == ("exceeded retry limit, last status: 429 Too Many Requests")
    assert reconciled.status == "failed"
    assert reconciled.error is not None
    assert reconciled.error.message == events[-1].error.message


async def test_codex_incomplete_stream_is_uncertain_and_not_attachable(tmp_path: Path) -> None:
    sdk = _Sdk(_IncompleteTurn())
    runtime = _runtime(tmp_path, sdk)
    request = _request(tmp_path)
    session = await runtime.prepare_session(request)
    handle = await runtime.start_turn(request, session)

    with pytest.raises(AgentRuntimeError) as captured:
        _ = [
            event
            async for event in validate_agent_stream(
                runtime.stream(handle),
                execution_id=request.identity.execution_id,
                provider_session_id=handle.provider_session_id,
            )
        ]

    reconciled = await runtime.reconcile(
        AgentReconcileRequest(
            execution_id=request.identity.execution_id,
            provider_session_id=handle.provider_session_id,
            provider_turn_id=handle.provider_turn_id,
            last_sequence=1,
        )
    )
    assert captured.value.reconciliation_required is True
    assert reconciled.status == "uncertain"
    assert reconciled.attachable is False


async def test_codex_rejects_unenforced_network_deny_policy(tmp_path: Path) -> None:
    sdk = _Sdk()
    runtime = _runtime(tmp_path, sdk)
    request = _request(tmp_path).model_copy(
        update={
            "policy": AgentPolicyContext(
                sandbox_mode="read_only",
                network_policy="deny",
            )
        }
    )

    with pytest.raises(AgentRuntimeError) as captured:
        await runtime.validate_request(request)

    assert captured.value.code == "agent.network_policy_unenforced"
    assert sdk.thread.turn_calls == []


async def test_codex_restricted_runtime_applies_network_deny_overrides(tmp_path: Path) -> None:
    sdk = _Sdk()
    runtime = _runtime(tmp_path, sdk, network_deny_is_enforced=True)
    request = _request(tmp_path).model_copy(
        update={
            "policy": AgentPolicyContext(
                sandbox_mode="read_only",
                network_policy="deny",
            )
        }
    )

    await runtime.prepare_session(request)

    assert sdk.configs[-1]["config_overrides"] == (
        'web_search="disabled"',
        "tools.web_search=false",
        "sandbox_workspace_write.network_access=false",
    )
