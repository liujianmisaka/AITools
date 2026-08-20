from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import cast

from misaka_approval_capability import DecisionDenied, DecisionGate, DecisionRequired
from misaka_capability_catalog import matches_json_schema
from misaka_interaction_contracts import decision_fingerprint
from misaka_kernel import HostContext
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel_contracts import (
    JsonObject,
    JsonValue,
    ModuleId,
    ModuleManifest,
    ServiceProvision,
    ServiceRequirement,
)
from misaka_policy_contracts import PolicyEffect, PolicyProvider, PolicyRequest
from misaka_resource_contracts import (
    CredentialProvider,
    FilesystemAccess,
    NetworkAccess,
    ResolvedCredential,
    ResourceLease,
    ResourceLeaseProvider,
    SandboxProvider,
    SandboxRequirements,
    SettingsProvider,
    SettingsSnapshot,
    SubprocessAccess,
)

from misaka_tool_capability.contracts import (
    TOOL_PIPELINE_SERVICE,
    TOOL_PROVIDER_SERVICE,
    ToolDescriptor,
    ToolExecutionContext,
    ToolExecutionRequest,
    ToolProvider,
    ToolResult,
    ToolStatus,
)

TOOL_PIPELINE_MODULE_ID = ModuleId("capability.tool.pipeline")


class ToolExecutionPipeline:
    def __init__(
        self,
        provider: ToolProvider,
        *,
        policy: PolicyProvider,
        decision_gate: DecisionGate,
        leases: ResourceLeaseProvider,
        sandbox: SandboxProvider,
        credentials: CredentialProvider | None = None,
        settings: SettingsProvider | None = None,
    ) -> None:
        self.provider = provider
        self.policy = policy
        self.decision_gate = decision_gate
        self.leases = leases
        self.sandbox = sandbox
        self.credentials = credentials
        self.settings = settings
        self._results: dict[str, tuple[str, ToolResult]] = {}
        self._pending: dict[str, tuple[str, asyncio.Task[ToolResult]]] = {}
        self._tasks_by_invocation: dict[str, asyncio.Task[ToolResult]] = {}
        self._completion_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    async def execute(self, request: ToolExecutionRequest) -> ToolResult:
        fingerprint = _request_fingerprint(request)
        async with self._lock:
            if self._closed:
                return _result(
                    request,
                    ToolStatus.REJECTED,
                    "tool.pipeline_closed",
                    "tool execution pipeline is closed",
                )
            existing = self._results.get(request.invocation.idempotency_key)
            if existing is not None:
                previous_fingerprint, result = existing
                if previous_fingerprint != fingerprint:
                    return _result(
                        request,
                        ToolStatus.REJECTED,
                        "tool.idempotency_conflict",
                        "idempotency key was reused for another tool execution plan",
                    )
                return result
            pending = self._pending.get(request.invocation.idempotency_key)
            if pending is not None:
                previous_fingerprint, task = pending
                if previous_fingerprint != fingerprint:
                    return _result(
                        request,
                        ToolStatus.REJECTED,
                        "tool.idempotency_conflict",
                        "idempotency key was reused for another tool execution plan",
                    )
            else:
                task = asyncio.create_task(self._execute_once(request))
                self._pending[request.invocation.idempotency_key] = (fingerprint, task)
            self._tasks_by_invocation[request.invocation.invocation_id] = task
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                result = _result(
                    request,
                    ToolStatus.RECONCILIATION_REQUIRED,
                    "tool.pipeline_interrupted",
                    "tool pipeline stopped before external execution state was proven",
                )
                await self._complete_task(request, fingerprint, task, result)
                return result
            completion = asyncio.create_task(self._complete_after_wait(request, fingerprint, task))
            self._completion_tasks.add(completion)
            completion.add_done_callback(self._completion_tasks.discard)
            return _result(
                request,
                ToolStatus.RECONCILIATION_REQUIRED,
                "tool.pipeline_interrupted",
                "tool pipeline stopped before external execution state was proven",
            )
        except Exception as exc:
            result = _result(
                request,
                ToolStatus.RECONCILIATION_REQUIRED,
                "tool.pipeline_failed",
                str(exc) or exc.__class__.__name__,
            )
            await self._complete_task(request, fingerprint, task, result)
            return result
        await self._complete_task(request, fingerprint, task, result)
        return result

    async def _complete_after_wait(
        self,
        request: ToolExecutionRequest,
        fingerprint: str,
        task: asyncio.Task[ToolResult],
    ) -> None:
        try:
            result = await task
        except asyncio.CancelledError:
            result = _result(
                request,
                ToolStatus.RECONCILIATION_REQUIRED,
                "tool.pipeline_interrupted",
                "tool pipeline stopped before external execution state was proven",
            )
        except Exception as exc:
            result = _result(
                request,
                ToolStatus.RECONCILIATION_REQUIRED,
                "tool.pipeline_failed",
                str(exc) or exc.__class__.__name__,
            )
        await self._complete_task(request, fingerprint, task, result)

    async def _complete_task(
        self,
        request: ToolExecutionRequest,
        fingerprint: str,
        task: asyncio.Task[ToolResult],
        result: ToolResult,
    ) -> None:
        async with self._lock:
            current = self._pending.get(request.invocation.idempotency_key)
            if current is None or current[1] is not task:
                return
            self._pending.pop(request.invocation.idempotency_key, None)
            self._results.setdefault(
                request.invocation.idempotency_key,
                (fingerprint, result),
            )
            for invocation_id, mapped_task in tuple(self._tasks_by_invocation.items()):
                if mapped_task is task:
                    self._tasks_by_invocation.pop(invocation_id, None)

    async def cancel(self, invocation_id: str, reason: str) -> None:
        if not invocation_id.strip() or not reason.strip():
            raise ValueError("invocation id and cancellation reason must not be empty")
        await self.provider.cancel(invocation_id, reason)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = tuple(set(self._tasks_by_invocation.values()))
            completion_tasks = tuple(self._completion_tasks)
        await self.provider.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if completion_tasks:
            await asyncio.gather(*completion_tasks, return_exceptions=True)

    async def _execute_once(self, request: ToolExecutionRequest) -> ToolResult:
        descriptor = await self._preflight(request)
        if isinstance(descriptor, ToolResult):
            return descriptor

        policy_decision = await self.policy.evaluate(
            PolicyRequest(request.proposal, context=request.policy_context)
        )
        if policy_decision.effect is PolicyEffect.DENY:
            return _result(
                request,
                ToolStatus.REJECTED,
                "policy.denied",
                policy_decision.reason,
            )
        if policy_decision.effect is PolicyEffect.REQUIRE_DECISION:
            try:
                await self.decision_gate.authorize(request.proposal)
            except DecisionRequired as exc:
                return _result(
                    request,
                    ToolStatus.REJECTED,
                    "policy.decision_required",
                    str(exc),
                )
            except DecisionDenied as exc:
                return _result(
                    request,
                    ToolStatus.REJECTED,
                    "policy.decision_denied",
                    str(exc),
                )

        if descriptor.destructive and not request.lease_requests:
            return _result(
                request,
                ToolStatus.REJECTED,
                "tool.resource_lease_required",
                "destructive tools require at least one fenced resource lease",
            )

        try:
            effective_sandbox = _apply_policy_constraints(
                request.sandbox,
                policy_decision.constraints,
            )
        except ValueError as exc:
            return _result(
                request,
                ToolStatus.REJECTED,
                "policy.constraints_invalid",
                str(exc),
            )

        acquired: list[ResourceLease] = []
        result: ToolResult | None = None
        try:
            try:
                for lease_request in request.lease_requests:
                    acquired.append(await self.leases.acquire(lease_request))
                sandbox = await self.sandbox.resolve(effective_sandbox)
                resolved_credentials: tuple[ResolvedCredential, ...] = ()
                if request.credential_refs:
                    if self.credentials is None:
                        result = _result(
                            request,
                            ToolStatus.REJECTED,
                            "credential.provider_unavailable",
                            "tool requests credentials but no credential provider is configured",
                        )
                    else:
                        resolved_credentials = tuple(
                            [await self.credentials.resolve(ref) for ref in request.credential_refs]
                        )
                resolved_settings: tuple[SettingsSnapshot, ...] = ()
                if request.settings_ids:
                    if self.settings is None:
                        result = _result(
                            request,
                            ToolStatus.REJECTED,
                            "settings.provider_unavailable",
                            "tool requests settings but no settings provider is configured",
                        )
                    else:
                        resolved_settings = tuple(
                            [
                                await self.settings.get(settings_id)
                                for settings_id in request.settings_ids
                            ]
                        )
                if not (request.credential_refs and self.credentials is None) and not (
                    request.settings_ids and self.settings is None
                ):
                    context = ToolExecutionContext(
                        owner=request.proposal.created_by,
                        scope=request.proposal.scope,
                        sandbox=sandbox,
                        leases=tuple(acquired),
                        credentials=resolved_credentials,
                        settings=resolved_settings,
                    )
                    result = await self._execute_provider(request, descriptor, context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", "tool.admission_failed")
                result = _result(
                    request,
                    ToolStatus.REJECTED,
                    str(code),
                    str(exc) or exc.__class__.__name__,
                )
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            result = _result(
                request,
                ToolStatus.RECONCILIATION_REQUIRED,
                "tool.pipeline_failed",
                str(exc) or exc.__class__.__name__,
            )
        finally:
            release_error = await _release_after_cancellation(self._release, acquired)
            if release_error is not None:
                result = _result(
                    request,
                    ToolStatus.RECONCILIATION_REQUIRED,
                    "tool.resource_release_uncertain",
                    release_error,
                )
        if result is None:
            return _result(
                request,
                ToolStatus.RECONCILIATION_REQUIRED,
                "tool.pipeline_no_result",
                "tool pipeline terminated without a result",
            )
        return result

    async def _preflight(
        self,
        request: ToolExecutionRequest,
    ) -> ToolDescriptor | ToolResult:
        descriptor = next(
            (
                item
                for item in await self.provider.tools()
                if item.tool_id == request.invocation.tool_id
            ),
            None,
        )
        if descriptor is None:
            return _result(
                request,
                ToolStatus.REJECTED,
                "tool.not_found",
                "tool was not found",
            )
        if not matches_json_schema(request.invocation.arguments, descriptor.input_schema):
            return _result(
                request,
                ToolStatus.REJECTED,
                "tool.input_contract_violated",
                "tool arguments do not satisfy input_schema",
            )
        return descriptor

    async def _execute_provider(
        self,
        request: ToolExecutionRequest,
        descriptor: ToolDescriptor,
        context: ToolExecutionContext,
    ) -> ToolResult:
        try:
            result = await self.provider.execute(request.invocation, context)
        except Exception as exc:
            message = _redact_text(str(exc) or exc.__class__.__name__, context)
            return _result(
                request,
                ToolStatus.RECONCILIATION_REQUIRED,
                "tool.provider_state_unknown",
                message,
            )
        if (
            result.invocation_id != request.invocation.invocation_id
            or result.tool_id != request.invocation.tool_id
        ):
            return _result(
                request,
                ToolStatus.RECONCILIATION_REQUIRED,
                "tool.provider_result_mismatch",
                "tool provider returned a result for another invocation",
            )
        if result.status is not ToolStatus.SUCCEEDED:
            if result.error_message is None:
                return result
            return replace(result, error_message=_redact_text(result.error_message, context))
        if _contains_resolved_secret(result.output, context):
            return _result(
                request,
                ToolStatus.FAILED,
                "tool.secret_output_forbidden",
                "tool output contains resolved credential material",
            )
        try:
            json.dumps(result.output, ensure_ascii=False)
        except (TypeError, ValueError):
            return _result(
                request,
                ToolStatus.FAILED,
                "tool.output_not_json",
                "tool provider returned a non-JSON value",
            )
        if not matches_json_schema(result.output, descriptor.output_schema):
            return _result(
                request,
                ToolStatus.FAILED,
                "tool.output_contract_violated",
                "tool output does not satisfy output_schema",
            )
        return result

    async def _release(self, acquired: list[ResourceLease]) -> str | None:
        errors: list[str] = []
        for lease in reversed(acquired):
            try:
                await self.leases.release(lease)
            except Exception as exc:
                errors.append(str(exc) or exc.__class__.__name__)
        return "; ".join(errors) if errors else None


async def _release_after_cancellation(
    release: Callable[[list[ResourceLease]], Awaitable[str | None]],
    acquired: list[ResourceLease],
) -> str | None:
    if not acquired:
        return None

    async def run_release() -> str | None:
        return await release(acquired)

    task: asyncio.Task[str | None] = asyncio.create_task(run_release())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_consume_task_exception)
            return "resource lease release was interrupted by cancellation"


def _consume_task_exception(task: asyncio.Task[str | None]) -> None:
    if not task.cancelled():
        task.exception()


class ToolExecutionPipelineModule:
    """Binds a preconfigured pipeline into the composition host."""

    def __init__(self, pipeline: ToolExecutionPipeline) -> None:
        self.pipeline = pipeline

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=TOOL_PIPELINE_MODULE_ID,
            version="1.0.0",
            requires=(ServiceRequirement(TOOL_PROVIDER_SERVICE, version="1.0.0"),),
            provides=(ServiceProvision(TOOL_PIPELINE_SERVICE, "1.0.0"),),
        )

    async def attach(self, context: HostContext) -> AsyncDisposer | None:
        provider = context.require(TOOL_PROVIDER_SERVICE)
        if provider is not self.pipeline.provider:
            raise ValueError("tool pipeline provider does not match the host binding")
        context.provide(TOOL_PIPELINE_SERVICE, self.pipeline, version="1.0.0")

        async def dispose() -> None:
            await self.pipeline.close()

        return dispose

    async def start(self, context: HostContext) -> None:
        del context


def _apply_policy_constraints(
    requested: SandboxRequirements,
    constraints: JsonObject,
) -> SandboxRequirements:
    filesystem = requested.filesystem
    network = requested.network
    subprocess = requested.subprocess
    allowed_tools = requested.allowed_tools

    raw_filesystem = constraints.get("filesystem")
    if raw_filesystem is not None:
        if not isinstance(raw_filesystem, str):
            raise ValueError("policy filesystem constraint must be a string")
        ceiling = FilesystemAccess(raw_filesystem)
        filesystem = min(
            (filesystem, ceiling),
            key=lambda item: {
                FilesystemAccess.NONE: 0,
                FilesystemAccess.READ_ONLY: 1,
                FilesystemAccess.WRITE: 2,
            }[item],
        )
    raw_network = constraints.get("network")
    if raw_network is not None:
        if not isinstance(raw_network, str):
            raise ValueError("policy network constraint must be a string")
        if NetworkAccess(raw_network) is NetworkAccess.DENY:
            network = NetworkAccess.DENY
    raw_subprocess = constraints.get("subprocess")
    if raw_subprocess is not None:
        if not isinstance(raw_subprocess, str):
            raise ValueError("policy subprocess constraint must be a string")
        if SubprocessAccess(raw_subprocess) is SubprocessAccess.DENY:
            subprocess = SubprocessAccess.DENY
    raw_tools = constraints.get("allowed_tools")
    if raw_tools is not None:
        if not isinstance(raw_tools, list) or not all(isinstance(item, str) for item in raw_tools):
            raise ValueError("policy allowed_tools constraint must be a string array")
        allowed_set = set(cast(list[str], raw_tools))
        allowed_tools = tuple(tool for tool in allowed_tools if tool in allowed_set)
    return replace(
        requested,
        filesystem=filesystem,
        network=network,
        subprocess=subprocess,
        allowed_tools=allowed_tools,
    )


def _request_fingerprint(request: ToolExecutionRequest) -> str:
    payload = {
        "tool_id": request.invocation.tool_id,
        "arguments": request.invocation.arguments,
        "proposal": decision_fingerprint(request.proposal),
        "sandbox": {
            "filesystem": request.sandbox.filesystem.value,
            "network": request.sandbox.network.value,
            "subprocess": request.sandbox.subprocess.value,
            "allowed_tools": request.sandbox.allowed_tools,
        },
        "leases": [
            {
                "type": item.resource.resource_type,
                "id": item.resource.resource_id,
                "scope": item.resource.scope.scope_id,
                "owner": item.owner.principal_id,
                "operation": item.operation_id,
                "ttl": item.ttl_seconds,
            }
            for item in request.lease_requests
        ],
        "credentials": [
            {"id": item.credential_id, "purpose": item.purpose} for item in request.credential_refs
        ],
        "settings": request.settings_ids,
        "policy_context": request.policy_context,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _contains_resolved_secret(value: JsonValue | None, context: ToolExecutionContext) -> bool:
    secrets = {credential.secret.reveal() for credential in context.credentials}
    if not secrets:
        return False
    if isinstance(value, str):
        return value in secrets
    if isinstance(value, dict):
        return any(_contains_resolved_secret(item, context) for item in value.values())
    if isinstance(value, list):
        return any(_contains_resolved_secret(item, context) for item in value)
    return False


def _redact_text(value: str, context: ToolExecutionContext) -> str:
    redacted = value
    for credential in context.credentials:
        redacted = redacted.replace(credential.secret.reveal(), "<redacted>")
    return redacted


def _result(
    request: ToolExecutionRequest,
    status: ToolStatus,
    error_code: str,
    error_message: str,
) -> ToolResult:
    return ToolResult(
        invocation_id=request.invocation.invocation_id,
        tool_id=request.invocation.tool_id,
        status=status,
        error_code=error_code,
        error_message=error_message,
    )
