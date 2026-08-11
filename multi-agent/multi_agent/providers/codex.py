from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from multi_agent.domain.errors import ProviderExecutionError, ProviderUnavailableError
from multi_agent.domain.json_schema import validate_codex_output_schema
from multi_agent.domain.models import (
    AccessMode,
    EventKind,
    ExecutionRequest,
    ProviderCapabilities,
    ProviderEvent,
    ProviderSessionRef,
)
from multi_agent.providers.base import AgentProvider, ExecutionHandle
from multi_agent.providers.catalog import (
    CodexModelCatalog,
    ProviderModelSpec,
    catalog_from_app_server,
)
from multi_agent.providers.runtime import CodexRuntimeDescriptor, CodexRuntimeLocator
from multi_agent.providers.utils import (
    extract_text,
    native_event_type,
    redact_payload,
    to_plain_data,
)


@dataclass(slots=True)
class _CodexTaskRuntime:
    turn: Any
    client: Any
    owns_client: bool
    closer: Any
    closed: bool = False

    async def stream(self) -> AsyncIterator[Any]:
        try:
            async for event in self.turn.stream():
                yield event
        finally:
            await self.closer(self)

    async def steer(self, prompt: str) -> None:
        await self.turn.steer(prompt)

    async def interrupt(self) -> None:
        try:
            await self.turn.interrupt()
        finally:
            await self.closer(self)


class CodexProvider(AgentProvider):
    name = "codex"
    _ALLOWED_OPTIONS = {
        "approval_mode",
        "base_instructions",
        "developer_instructions",
        "effort",
        "model",
        "personality",
        "service_tier",
        "summary",
    }

    def __init__(
        self,
        client: Any | None = None,
        sdk_module: Any | None = None,
        *,
        codex_bin: str | None = None,
        codex_home: str | None = None,
        model_catalog: CodexModelCatalog | None = None,
        runtime_locator: CodexRuntimeLocator | None = None,
        catalog_ttl_seconds: float = 15.0,
        catalog_timeout_seconds: float = 20.0,
    ) -> None:
        self._client = client
        self._sdk = sdk_module
        self._runtime_locator = runtime_locator or CodexRuntimeLocator(
            codex_bin=codex_bin,
            codex_home=codex_home,
        )
        self._model_catalog = model_catalog
        self._fixed_model_catalog = model_catalog is not None
        self._catalog_signature: tuple[object, ...] | None = None
        self._catalog_loaded_at = 0.0
        self._catalog_ttl_seconds = max(0.0, catalog_ttl_seconds)
        self._catalog_timeout_seconds = max(0.1, catalog_timeout_seconds)
        self._catalog_lock = asyncio.Lock()
        self._active_clients: dict[int, Any] = {}

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            resume_session=True,
            stream_events=True,
            steer_running_turn=True,
            cancel_running_turn=True,
            structured_output=True,
            approval_callback=False,
            read_only_mode=True,
            workspace_write_mode=True,
        )

    async def models(self) -> tuple[ProviderModelSpec, ...]:
        return (await self._catalog()).models

    async def metadata(self) -> dict[str, object]:
        catalog = await self._catalog()
        return {
            "model_provider": catalog.provider_id,
            "model_catalog": "codex_app_server",
            "model_count": len(catalog.models),
            "runtime_id": catalog.runtime_id,
            "environment_kind": catalog.environment_kind,
            "config_source": catalog.config_source,
            "catalog_revision": catalog.revision,
            "catalog_path": (
                str(catalog.catalog_path) if catalog.catalog_path is not None else None
            ),
        }

    async def _discover_catalog(
        self,
        runtime: CodexRuntimeDescriptor,
    ) -> CodexModelCatalog:
        await self.start()
        assert self._sdk is not None
        config = self._client_config(self._sdk, runtime)
        client = self._sdk.AsyncCodex(config)
        entered_client: Any | None = None
        try:
            entered_client = await client.__aenter__()
            models_method = getattr(entered_client, "models", None)
            if models_method is None:
                raise ProviderUnavailableError(
                    "installed Codex SDK does not expose AsyncCodex.models"
                )
            response = await models_method(include_hidden=False)
            return catalog_from_app_server(runtime, response)
        finally:
            if entered_client is not None:
                await client.__aexit__(None, None, None)
            elif hasattr(client, "close"):
                await client.close()

    async def _catalog(self, *, force_refresh: bool = False) -> CodexModelCatalog:
        if self._fixed_model_catalog:
            assert self._model_catalog is not None
            return self._model_catalog

        try:
            runtime = self._runtime_locator.resolve()
        except ProviderUnavailableError:
            raise
        except Exception as exc:
            raise ProviderUnavailableError(
                f"cannot resolve Codex runtime: {exc}"
            ) from exc
        now = time.monotonic()
        if (
            not force_refresh
            and self._model_catalog is not None
            and self._catalog_signature == runtime.signature
            and now - self._catalog_loaded_at < self._catalog_ttl_seconds
        ):
            return self._model_catalog

        async with self._catalog_lock:
            try:
                runtime = self._runtime_locator.resolve()
                now = time.monotonic()
                if (
                    not force_refresh
                    and self._model_catalog is not None
                    and self._catalog_signature == runtime.signature
                    and now - self._catalog_loaded_at < self._catalog_ttl_seconds
                ):
                    return self._model_catalog
                catalog = await asyncio.wait_for(
                    self._discover_catalog(runtime),
                    timeout=self._catalog_timeout_seconds,
                )
            except ProviderUnavailableError:
                raise
            except TimeoutError as exc:
                raise ProviderUnavailableError(
                    "Codex model discovery timed out"
                ) from exc
            except Exception as exc:
                raise ProviderUnavailableError(
                    f"cannot discover Codex models from {runtime.codex_home}: {exc}"
                ) from exc
            self._catalog_signature = runtime.signature
            self._catalog_loaded_at = time.monotonic()
            self._model_catalog = catalog
            return catalog

    async def start(self) -> None:
        if self._sdk is None:
            try:
                import openai_codex as sdk
            except ImportError as exc:
                raise ProviderUnavailableError(
                    "Codex provider requires the optional package 'openai-codex'"
                ) from exc
            self._sdk = sdk

    async def _task_client(
        self,
        runtime: CodexRuntimeDescriptor | None,
    ) -> tuple[Any, bool]:
        if self._client is not None:
            return self._client, False
        assert self._sdk is not None
        config = self._client_config(self._sdk, runtime)
        client = self._sdk.AsyncCodex(config)
        try:
            await client.__aenter__()
        except BaseException:
            if hasattr(client, "close"):
                try:
                    await client.close()
                except Exception:
                    pass
            raise
        self._active_clients[id(client)] = client
        return client, True

    async def _close_task_runtime(self, runtime: _CodexTaskRuntime) -> None:
        if not runtime.owns_client or runtime.closed:
            return
        runtime.closed = True
        self._active_clients.pop(id(runtime.client), None)
        await runtime.client.__aexit__(None, None, None)

    def _client_config(
        self,
        sdk: Any,
        runtime: CodexRuntimeDescriptor | None = None,
    ) -> Any:
        config_factory = getattr(sdk, "CodexConfig", None)
        if config_factory is None:
            raise ProviderUnavailableError("installed Codex SDK does not expose CodexConfig")
        if runtime is None:
            try:
                runtime = self._runtime_locator.resolve()
            except Exception as exc:
                raise ProviderUnavailableError(
                    f"cannot resolve Codex runtime: {exc}"
                ) from exc
        config_kwargs: dict[str, Any] = {}
        if runtime.codex_bin is not None:
            config_kwargs["codex_bin"] = runtime.codex_bin
        config_kwargs["env"] = {"CODEX_HOME": str(runtime.codex_home)}
        return config_factory(**config_kwargs)

    async def close(self) -> None:
        clients = list(self._active_clients.values())
        self._active_clients.clear()
        if clients:
            results = await asyncio.gather(
                *(client.__aexit__(None, None, None) for client in clients),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise ExceptionGroup("errors while closing Codex task clients", errors)

    def _enum(self, owner: str, member: str, fallback: str) -> Any:
        enum_owner = getattr(self._sdk, owner, None) if self._sdk is not None else None
        return getattr(enum_owner, member, fallback)

    @staticmethod
    def _without_none(values: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in values.items() if value is not None}

    async def _model_options(
        self,
        options: dict[str, Any],
    ) -> tuple[str, CodexModelCatalog]:
        model = options.get("model")
        if model is not None and not isinstance(model, str):
            raise ProviderExecutionError(
                "Codex model must be a string",
                code="invalid_provider_options",
            )
        if not isinstance(model, str) or not model.strip():
            raise ProviderExecutionError(
                "Codex model must be explicitly selected",
                code="invalid_provider_options",
            )
        catalog = await self._catalog(force_refresh=True)
        selected_model = next(
            (candidate for candidate in catalog.models if candidate.id == model),
            None,
        )
        if selected_model is None:
            raise ProviderExecutionError(
                f"Codex model is not in the configured catalog: {model}",
                code="invalid_provider_options",
            )
        effort = options.get("effort")
        if not isinstance(effort, str) or not effort.strip():
            raise ProviderExecutionError(
                "Codex effort must be explicitly selected",
                code="invalid_provider_options",
            )
        if selected_model.efforts and effort not in selected_model.efforts:
            raise ProviderExecutionError(
                f"Codex effort {effort!r} is not allowed for model {model!r}",
                code="invalid_provider_options",
            )
        return model, catalog

    @staticmethod
    def _turn_error_message(payload: dict[str, Any]) -> str | None:
        turn = payload.get("turn")
        if not isinstance(turn, dict):
            return None
        error = turn.get("error")
        if not isinstance(error, dict):
            return None
        message = error.get("message")
        return str(message) if message else None

    @staticmethod
    def _final_response(payload: dict[str, Any]) -> str | None:
        for key in ("final_response", "finalResponse"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _agent_message(payload: dict[str, Any]) -> str | None:
        item = payload.get("item")
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type", "")).replace("_", "").lower()
        if item_type != "agentmessage":
            return None
        text = item.get("text")
        if isinstance(text, str) and text:
            return text
        content = item.get("content")
        if not isinstance(content, list):
            return None
        parts = [
            str(block["text"])
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "".join(parts) if parts else None

    async def start_execution(
        self,
        request: ExecutionRequest,
        session: ProviderSessionRef | None = None,
    ) -> ExecutionHandle:
        await self.start()
        unknown = set(request.provider_options) - self._ALLOWED_OPTIONS
        if unknown:
            raise ProviderExecutionError(
                f"unsupported Codex provider options: {sorted(unknown)}",
                code="invalid_provider_options",
            )

        options = request.provider_options
        model, catalog = await self._model_options(options)
        if request.output_schema is not None:
            try:
                validate_codex_output_schema(request.output_schema)
            except ValueError as exc:
                raise ProviderExecutionError(
                    f"invalid Codex output_schema: {exc}",
                    code="invalid_output_schema",
                ) from exc
        sandbox_member = (
            "read_only" if request.access == AccessMode.read_only else "workspace_write"
        )
        sandbox = self._enum("Sandbox", sandbox_member, request.access.value)
        approval_name = str(options.get("approval_mode", "deny_all"))
        if approval_name not in {"deny_all", "auto_review"}:
            raise ProviderExecutionError(
                "Codex approval_mode must be deny_all or auto_review",
                code="invalid_provider_options",
            )
        approval = self._enum("ApprovalMode", approval_name, approval_name)
        thread_kwargs = self._without_none(
            {
                "approval_mode": approval,
                "base_instructions": options.get("base_instructions"),
                "cwd": str(request.workspace),
                "developer_instructions": options.get("developer_instructions"),
                "model": model,
                "personality": options.get("personality"),
                "sandbox": sandbox,
                "service_tier": options.get("service_tier"),
            }
        )
        client, owns_client = await self._task_client(catalog.runtime)
        try:
            if session is None:
                thread = await client.thread_start(ephemeral=False, **thread_kwargs)
            else:
                thread = await client.thread_resume(session.session_id, **thread_kwargs)

            turn_kwargs = self._without_none(
                {
                    "approval_mode": approval,
                    "cwd": str(request.workspace),
                    "effort": options.get("effort"),
                    "model": model,
                    "output_schema": request.output_schema,
                    "personality": options.get("personality"),
                    "sandbox": sandbox,
                    "service_tier": options.get("service_tier"),
                    "summary": options.get("summary"),
                }
            )
            turn = await thread.turn(request.prompt, **turn_kwargs)
        except BaseException:
            if owns_client:
                self._active_clients.pop(id(client), None)
                await client.__aexit__(None, None, None)
            raise
        return ExecutionHandle(
            session=ProviderSessionRef(provider=self.name, session_id=str(thread.id)),
            native=_CodexTaskRuntime(
                turn=turn,
                client=client,
                owns_client=owns_client,
                closer=self._close_task_runtime,
            ),
        )

    async def stream(self, handle: ExecutionHandle) -> AsyncIterator[ProviderEvent]:
        message_deltas: list[str] = []
        completed_message: str | None = None
        async for native in handle.native.stream():
            event_type = native_event_type(native)
            payload_source = getattr(native, "payload", native)
            payload = redact_payload(payload_source)
            text = extract_text(payload_source)
            lowered = event_type.lower()
            if "turn/completed" in lowered or lowered.endswith("turn.completed"):
                status = str(to_plain_data(payload).get("turn", {}).get("status", "")) if isinstance(payload, dict) else ""
                if "fail" in status.lower() or "error" in status.lower():
                    error_message = (
                        self._turn_error_message(payload)
                        if isinstance(payload, dict)
                        else None
                    )
                    yield ProviderEvent(
                        kind=EventKind.failed,
                        summary=error_message or text or "Codex turn failed",
                        payload=payload if isinstance(payload, dict) else {"value": payload},
                        raw_event_type=event_type,
                    )
                else:
                    final_output = (
                        self._final_response(payload)
                        if isinstance(payload, dict)
                        else None
                    )
                    final_output = (
                        final_output
                        or completed_message
                        or "".join(message_deltas)
                    )
                    yield ProviderEvent(
                        kind=EventKind.completed,
                        summary="Codex turn completed",
                        payload={
                            "final_output": final_output,
                            "native": payload,
                        },
                        raw_event_type=event_type,
                    )
            elif "agentmessage/delta" in lowered or lowered.endswith("message/delta"):
                delta = payload.get("delta") if isinstance(payload, dict) else None
                if not isinstance(delta, str):
                    delta = text
                if delta:
                    message_deltas.append(delta)
                yield ProviderEvent(
                    kind=EventKind.message_delta,
                    summary=delta or event_type,
                    payload=payload if isinstance(payload, dict) else {"value": payload},
                    raw_event_type=event_type,
                )
            elif "item/completed" in lowered and isinstance(payload, dict):
                agent_message = self._agent_message(payload)
                if agent_message is not None:
                    completed_message = agent_message
                    yield ProviderEvent(
                        kind=EventKind.message_completed,
                        summary=agent_message,
                        payload=payload,
                        raw_event_type=event_type,
                    )
            elif "tool" in lowered and ("start" in lowered or "begin" in lowered):
                yield ProviderEvent(
                    kind=EventKind.tool_started,
                    summary=text or event_type,
                    payload=payload if isinstance(payload, dict) else {"value": payload},
                    raw_event_type=event_type,
                )
            elif "tool" in lowered and ("complete" in lowered or "end" in lowered):
                yield ProviderEvent(
                    kind=EventKind.tool_completed,
                    summary=text or event_type,
                    payload=payload if isinstance(payload, dict) else {"value": payload},
                    raw_event_type=event_type,
                )
            elif text:
                yield ProviderEvent(
                    kind=EventKind.message_completed,
                    summary=text,
                    payload=payload if isinstance(payload, dict) else {"value": payload},
                    raw_event_type=event_type,
                )

    async def steer(self, handle: ExecutionHandle, prompt: str) -> None:
        await handle.native.steer(prompt)

    async def cancel(self, handle: ExecutionHandle) -> None:
        await handle.native.interrupt()
