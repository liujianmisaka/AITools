from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path

from multi_agent.domain.errors import ProviderExecutionError
from multi_agent.domain.models import AccessMode, ExecutionRequest
from multi_agent.providers.claude import ClaudeProvider
from multi_agent.providers.catalog import CodexModelCatalog, ProviderModelSpec
from multi_agent.providers.codex import CodexProvider
from multi_agent.providers.copilot import CopilotProvider


_CODEX_MODELS = (
    ProviderModelSpec(
        id="sensenova/deepseek-v4-flash",
        label="DeepSeek V4 Flash (SenseNova)",
        model_type="sensenova",
        efforts=("low", "medium", "high"),
        default_effort="medium",
    ),
)
_CODEX_CATALOG = CodexModelCatalog(
    provider_id="openai",
    config_path=Path("config.toml"),
    catalog_path=Path("opencodex-catalog.json"),
    models=_CODEX_MODELS,
)


def request(workspace: Path) -> ExecutionRequest:
    return ExecutionRequest(
        workflow_instance_id="instance",
        work_item_id="instance:task:1",
        logical_key="task",
        prompt="hello",
        role="worker",
        workspace=workspace,
        access=AccessMode.read_only,
    )


class _CodexTurn:
    async def stream(self):
        yield types.SimpleNamespace(
            method="turn/started",
            payload={"thread_id": "codex-session", "turn": {"status": "inProgress"}},
        )
        yield types.SimpleNamespace(
            method="item/agentMessage/delta",
            payload={
                "thread_id": "codex-session",
                "turn_id": "turn",
                "item_id": "item",
                "delta": "hi",
            },
        )
        yield types.SimpleNamespace(
            method="item/completed",
            payload={
                "thread_id": "codex-session",
                "turn_id": "turn",
                "item": {"id": "item", "type": "agentMessage", "text": "hi"},
            },
        )
        yield types.SimpleNamespace(
            method="turn/completed",
            payload={
                "thread_id": "codex-session",
                "turn": {"status": "completed"},
            },
        )

    async def steer(self, _prompt):
        return None

    async def interrupt(self):
        return None


class _IncompleteCodexTurn(_CodexTurn):
    async def stream(self):
        if False:
            yield None


class _CodexThread:
    id = "codex-session"

    def __init__(self, turn=None) -> None:
        self.turn_kwargs = None
        self.native_turn = turn or _CodexTurn()

    async def turn(self, prompt, **kwargs):
        self.turn_kwargs = (prompt, kwargs)
        return self.native_turn


class _CodexClient:
    def __init__(self) -> None:
        self.thread = _CodexThread()
        self.start_kwargs = None

    async def thread_start(self, **kwargs):
        self.start_kwargs = kwargs
        return self.thread


class _ClaudeOptions:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class AssistantMessage:
    def __init__(self, text: str) -> None:
        self.content = [{"text": text}]


class ResultMessage:
    def __init__(self, result: str) -> None:
        self.result = result
        self.is_error = False
        self.subtype = "success"


class _ClaudeClient:
    def __init__(self, options) -> None:
        self.options = options
        self.prompt = None
        self.disconnected = False

    async def connect(self):
        return None

    async def query(self, prompt):
        self.prompt = prompt

    async def get_server_info(self):
        return {"session_id": "claude-session"}

    async def receive_response(self):
        yield AssistantMessage("hello")
        yield ResultMessage("done")

    async def disconnect(self):
        self.disconnected = True

    async def interrupt(self):
        return None


class _IncompleteClaudeClient(_ClaudeClient):
    async def receive_response(self):
        yield AssistantMessage("partial")


class _FailingClaudeClient(_ClaudeClient):
    async def query(self, prompt):
        raise RuntimeError("query failed")


class _CopilotEvent:
    def __init__(self, event_type: str, data: dict) -> None:
        self.type = event_type
        self.data = data


class _CopilotSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.handler = None
        self.disconnected = False

    def on(self, handler):
        self.handler = handler
        return lambda: None

    async def send(self, _prompt, **_kwargs):
        self.handler(_CopilotEvent("assistant.message_delta", {"deltaContent": "hi"}))
        self.handler(_CopilotEvent("assistant.message", {"content": "hi"}))
        self.handler(_CopilotEvent("session.idle", {}))

    async def disconnect(self):
        self.disconnected = True

    async def abort(self):
        return None


class _CopilotClient:
    def __init__(self) -> None:
        self.kwargs = None

    async def create_session(self, *, session_id, **kwargs):
        self.kwargs = kwargs
        return _CopilotSession(session_id)


class _CodexConfig:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class _LifecycleCodexClient(_CodexClient):
    def __init__(self, config) -> None:
        super().__init__()
        self.config = config
        self.entered = False
        self.closed = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_exc_info):
        self.closed = True


class ProviderAdapterFakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_uses_one_configured_cli_per_task(self) -> None:
        clients = []

        def client_factory(config):
            client = _LifecycleCodexClient(config)
            clients.append(client)
            return client

        sdk = types.SimpleNamespace(
            AsyncCodex=client_factory,
            CodexConfig=_CodexConfig,
            Sandbox=types.SimpleNamespace(read_only="sandbox-read"),
            ApprovalMode=types.SimpleNamespace(deny_all="deny"),
        )
        provider = CodexProvider(
            sdk_module=sdk,
            codex_bin=sys.executable,
            codex_home=str(Path.home()),
            model_catalog=_CODEX_CATALOG,
        )

        execution_request = request(Path.home()).model_copy(
            update={
                "provider_options": {
                    "model": "sensenova/deepseek-v4-flash",
                    "effort": "high",
                }
            }
        )
        for _ in range(2):
            handle = await provider.start_execution(execution_request)
            _events = [event async for event in provider.stream(handle)]
        await provider.close()

        self.assertEqual(len(clients), 2)
        for client in clients:
            self.assertEqual(client.config.kwargs["codex_bin"], sys.executable)
            self.assertEqual(
                client.config.kwargs["env"]["CODEX_HOME"],
                str(Path.home().resolve()),
            )
            self.assertTrue(client.entered)
            self.assertTrue(client.closed)

    async def test_codex_maps_thread_turn_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            client = _CodexClient()
            sdk = types.SimpleNamespace(
                Sandbox=types.SimpleNamespace(read_only="sandbox-read"),
                ApprovalMode=types.SimpleNamespace(deny_all="deny"),
            )
            provider = CodexProvider(
                client=client,
                sdk_module=sdk,
                model_catalog=_CODEX_CATALOG,
            )
            execution_request = request(Path(temporary_directory)).model_copy(
                update={
                    "provider_options": {
                        "model": "sensenova/deepseek-v4-flash",
                        "effort": "high",
                    }
                }
            )
            handle = await provider.start_execution(execution_request)
            events = [event async for event in provider.stream(handle)]

        self.assertEqual(handle.session.session_id, "codex-session")
        self.assertEqual(client.start_kwargs["sandbox"], "sandbox-read")
        self.assertEqual(
            client.start_kwargs["model"],
            "sensenova/deepseek-v4-flash",
        )
        self.assertNotIn("model_provider", client.start_kwargs)
        self.assertEqual(
            client.thread.turn_kwargs[1]["model"],
            "sensenova/deepseek-v4-flash",
        )
        self.assertEqual(client.thread.turn_kwargs[1]["effort"], "high")
        self.assertEqual(events[-1].kind.value, "completed")
        self.assertEqual(events[-1].payload["final_output"], "hi")

    async def test_codex_does_not_invent_completion_for_incomplete_stream(self) -> None:
        client = _CodexClient()
        client.thread = _CodexThread(_IncompleteCodexTurn())
        provider = CodexProvider(
            client=client,
            sdk_module=types.SimpleNamespace(),
            model_catalog=_CODEX_CATALOG,
        )
        execution_request = request(Path.home()).model_copy(
            update={
                "provider_options": {
                    "model": "sensenova/deepseek-v4-flash",
                    "effort": "high",
                }
            }
        )

        handle = await provider.start_execution(execution_request)
        events = [event async for event in provider.stream(handle)]

        self.assertFalse(any(event.kind.value == "completed" for event in events))

    async def test_codex_rejects_task_level_provider_and_config_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            provider = CodexProvider(
                client=_CodexClient(),
                sdk_module=types.SimpleNamespace(),
                model_catalog=_CODEX_CATALOG,
            )
            for forbidden in ("model_provider", "config"):
                execution_request = request(Path(temporary_directory)).model_copy(
                    update={
                        "provider_options": {
                            "model": "sensenova/deepseek-v4-flash",
                            "effort": "high",
                            forbidden: "untrusted",
                        }
                    }
                )
                with self.assertRaisesRegex(
                    ProviderExecutionError,
                    "unsupported Codex provider options",
                ):
                    await provider.start_execution(execution_request)

    async def test_codex_requires_catalog_model_and_effort(self) -> None:
        provider = CodexProvider(
            client=_CodexClient(),
            sdk_module=types.SimpleNamespace(),
            model_catalog=_CODEX_CATALOG,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_request = request(Path(temporary_directory))
            with self.assertRaisesRegex(ProviderExecutionError, "explicitly selected"):
                await provider.start_execution(base_request)

            unknown_model = base_request.model_copy(
                update={
                    "provider_options": {
                        "model": "unknown/model",
                        "effort": "high",
                    }
                }
            )
            with self.assertRaisesRegex(ProviderExecutionError, "configured catalog"):
                await provider.start_execution(unknown_model)

            invalid_effort = base_request.model_copy(
                update={
                    "provider_options": {
                        "model": "sensenova/deepseek-v4-flash",
                        "effort": "ultra",
                    }
                }
            )
            with self.assertRaisesRegex(ProviderExecutionError, "not allowed"):
                await provider.start_execution(invalid_effort)

    async def test_codex_rejects_invalid_output_schema_before_starting_task(self) -> None:
        client = _CodexClient()
        provider = CodexProvider(
            client=client,
            sdk_module=types.SimpleNamespace(),
            model_catalog=_CODEX_CATALOG,
        )
        execution_request = request(Path.home()).model_copy(
            update={
                "provider_options": {
                    "model": "sensenova/deepseek-v4-flash",
                    "effort": "high",
                },
                "output_schema": {
                    "type": "object",
                    "required": ["result"],
                    "properties": {"result": {"type": "integer"}},
                },
            }
        )

        with self.assertRaisesRegex(
            ProviderExecutionError,
            "additionalProperties must be false",
        ):
            await provider.start_execution(execution_request)
        self.assertIsNone(client.start_kwargs)

    async def test_claude_maps_options_session_and_messages(self) -> None:
        clients = []

        def factory(*, options):
            client = _ClaudeClient(options)
            clients.append(client)
            return client

        sdk = types.SimpleNamespace()
        with tempfile.TemporaryDirectory() as temporary_directory:
            provider = ClaudeProvider(
                client_factory=factory,
                options_factory=_ClaudeOptions,
                sdk_module=sdk,
            )
            execution_request = request(Path(temporary_directory)).model_copy(
                update={
                    "output_schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                    }
                }
            )
            handle = await provider.start_execution(execution_request)
            events = [event async for event in provider.stream(handle)]

        self.assertEqual(handle.session.session_id, "claude-session")
        self.assertEqual(clients[0].prompt, "hello")
        self.assertIn("Write", clients[0].options.kwargs["disallowed_tools"])
        self.assertEqual(clients[0].options.kwargs["setting_sources"], [])
        self.assertTrue(clients[0].options.kwargs["sandbox"]["failIfUnavailable"])
        self.assertEqual(
            clients[0].options.kwargs["output_format"]["type"],
            "json_schema",
        )
        self.assertEqual(events[-1].kind.value, "completed")
        self.assertEqual(events[-1].payload["final_output"], "done")
        self.assertTrue(clients[0].disconnected)

    async def test_claude_does_not_invent_completion_for_incomplete_stream(self) -> None:
        clients = []

        def factory(*, options):
            client = _IncompleteClaudeClient(options)
            clients.append(client)
            return client

        provider = ClaudeProvider(
            client_factory=factory,
            options_factory=_ClaudeOptions,
            sdk_module=types.SimpleNamespace(),
        )
        handle = await provider.start_execution(request(Path.home()))
        events = [event async for event in provider.stream(handle)]

        self.assertFalse(any(event.kind.value == "completed" for event in events))
        self.assertTrue(clients[0].disconnected)

    async def test_claude_disconnects_when_initial_query_fails(self) -> None:
        clients = []

        def factory(*, options):
            client = _FailingClaudeClient(options)
            clients.append(client)
            return client

        provider = ClaudeProvider(
            client_factory=factory,
            options_factory=_ClaudeOptions,
            sdk_module=types.SimpleNamespace(),
        )

        with self.assertRaisesRegex(RuntimeError, "query failed"):
            await provider.start_execution(request(Path.home()))
        self.assertTrue(clients[0].disconnected)

    async def test_copilot_maps_session_callbacks_and_idle(self) -> None:
        client = _CopilotClient()
        with tempfile.TemporaryDirectory() as temporary_directory:
            provider = CopilotProvider(client=client)
            handle = await provider.start_execution(request(Path(temporary_directory)))
            events = [event async for event in provider.stream(handle)]

        self.assertEqual(client.kwargs["streaming"], True)
        self.assertEqual(client.kwargs["available_tools"], ["glob", "grep", "view"])
        self.assertEqual(events[-1].kind.value, "completed")
        self.assertEqual(events[-1].payload["final_output"], "hi")
