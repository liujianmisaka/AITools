from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity
from temporalio.exceptions import ApplicationError

from multi_agent_v2.packages.agent_runtime import (
    AgentCancelledEvent,
    AgentCompletedEvent,
    AgentEvent,
    AgentExecutionIdentity,
    AgentFailedEvent,
    AgentPolicyContext,
    AgentReconcileRequest,
    AgentResumeRequest,
    AgentRuntimeError,
    AgentRuntimeRegistry,
    AgentStartRequest,
    AgentTurnHandle,
    ReconcileResult,
    WorkspaceLease,
    validate_agent_stream,
)
from multi_agent_v2.packages.artifacts import EvidenceRecorder
from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.persistence import (
    CleanupClaimDisposition,
    ExecutionAttemptRegistration,
    ExecutionLeaseLost,
    ExecutionLeaseRepository,
    ExecutionRegistration,
    LeaseClaimDisposition,
    WorktreeCleanupClaim,
    WorktreeRegistration,
    WorktreeRepository,
)
from multi_agent_v2.packages.policy import PreparedWorkspace, WorkspaceSupervisor
from multi_agent_v2.packages.sandbox import SandboxRequirements
from multi_agent_v2.packages.workflow_dsl.ir import AgentExecutionIr
from multi_agent_v2.packages.workflow_runtime.activities import successful_activity_result
from multi_agent_v2.packages.workflow_runtime.messages import (
    NodeActivityRequest,
    NodeActivityResult,
)


class AgentHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    execution_id: str
    lease_epoch: int = Field(ge=1)
    phase: str
    last_sequence: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class AgentActivityInvocation:
    workflow_run_id: str
    activity_id: str
    attempt: int
    worker_id: str

    @property
    def activity_run_id(self) -> str:
        return f"{self.workflow_run_id}:{self.activity_id}:{self.attempt}"

    @property
    def lease_owner(self) -> str:
        identity = f"{self.worker_id}\0{self.activity_run_id}"
        return f"agent-worker:{hashlib.sha256(identity.encode()).hexdigest()}"

    @property
    def attempt_id(self) -> str:
        return hashlib.sha256(self.activity_run_id.encode()).hexdigest()


class AgentActivityRunner:
    """Owns the only path that may start an Agent turn for a workflow node."""

    def __init__(
        self,
        *,
        executions: ExecutionLeaseRepository,
        worktrees: WorktreeRepository,
        workspaces: WorkspaceSupervisor,
        runtimes: AgentRuntimeRegistry,
        evidence: EvidenceRecorder,
        lease_duration: timedelta = timedelta(seconds=30),
        heartbeat_interval: timedelta = timedelta(seconds=5),
        cleanup_lease_duration: timedelta = timedelta(minutes=2),
    ) -> None:
        if heartbeat_interval <= timedelta(0) or heartbeat_interval >= lease_duration:
            raise ValueError("heartbeat interval must be positive and shorter than lease duration")
        self._executions = executions
        self._worktrees = worktrees
        self._workspaces = workspaces
        self._runtimes = runtimes
        self._evidence = evidence
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._cleanup_lease_duration = cleanup_lease_duration

    async def execute(
        self,
        request: NodeActivityRequest,
        invocation: AgentActivityInvocation,
        *,
        heartbeat: Callable[[AgentHeartbeat], None],
    ) -> NodeActivityResult:
        execution = request.execution
        if not isinstance(execution, AgentExecutionIr):
            raise ApplicationError(
                "agent activity received a non-agent execution contract",
                type="AgentExecutionContractInvalid",
                non_retryable=True,
            )
        registration = self._registration(request, execution)
        claim = await self._executions.claim(
            registration,
            lease_owner=invocation.lease_owner,
            lease_duration=self._lease_duration,
        )
        if claim.disposition is LeaseClaimDisposition.TERMINAL:
            return self._cached_result(
                request,
                claim.status,
                claim.result_payload,
                claim.error_code,
            )
        if claim.disposition is LeaseClaimDisposition.BUSY:
            raise ApplicationError(
                "agent execution is owned by an active worker",
                type="AgentExecutionBusy",
            )
        runtime = self._runtimes.get(execution.provider)
        if claim.disposition is LeaseClaimDisposition.RECONCILIATION_REQUIRED:
            await self._record_evidence(
                request,
                invocation=None,
                event_type="reconciliation_required",
                payload={
                    "reason": "an earlier provider execution requires reconciliation",
                    "lastSequence": claim.last_sequence,
                },
                provider_session_id=claim.provider_session_id,
                provider_turn_id=claim.provider_turn_id,
            )
            reconciled = await runtime.reconcile(
                AgentReconcileRequest(
                    execution_id=request.execution_id,
                    provider_session_id=claim.provider_session_id,
                    provider_turn_id=claim.provider_turn_id,
                    phase="unknown",
                    last_sequence=claim.last_sequence,
                )
            )
            return await self._resolve_reconciliation(request, reconciled)

        lease_epoch = claim.lease_epoch
        await self._record_evidence(
            request,
            invocation=None,
            event_type="execution_registered",
            payload={
                "idempotencyKey": request.idempotency_key,
                "planHash": request.plan_hash,
                "outputSchemaSha256": request.output_schema.sha256,
            },
        )
        await self._record_evidence(
            request,
            invocation=None,
            event_type="lease_acquired",
            payload={"leaseEpoch": lease_epoch},
        )
        await self._executions.begin_attempt(
            ExecutionAttemptRegistration(
                attempt_id=invocation.attempt_id,
                execution_id=request.execution_id,
                temporal_workflow_run_id=invocation.workflow_run_id,
                temporal_activity_id=invocation.activity_id,
                temporal_activity_run_id=invocation.activity_run_id,
                attempt_number=invocation.attempt,
                worker_id=invocation.worker_id,
            ),
            lease_owner=invocation.lease_owner,
            lease_epoch=lease_epoch,
        )
        await self._record_evidence(
            request,
            invocation=invocation,
            event_type="attempt_started",
            payload={"attempt": invocation.attempt, "workerId": invocation.worker_id},
        )

        prepared: PreparedWorkspace | None = None
        handle: AgentTurnHandle | None = None
        start_intent_durable = False
        preserve_workspace = False
        try:
            prepared = await self._prepare_workspace(request, execution)
            await self._record_evidence(
                request,
                invocation=invocation,
                event_type="workspace_prepared",
                payload={
                    "workspaceId": prepared.workspace_id,
                    "access": prepared.access,
                    "isolated": prepared.owns_worktree,
                    "worktreeId": (prepared.path.name if prepared.owns_worktree else None),
                },
            )
            agent_request = self._agent_request(request, execution, prepared, invocation.attempt)
            await runtime.validate_request(agent_request)
            await self._executions.mark_start_intent(
                request.execution_id,
                lease_owner=invocation.lease_owner,
                lease_epoch=lease_epoch,
            )
            start_intent_durable = True
            preserve_workspace = True
            await self._record_evidence(
                request,
                invocation=invocation,
                event_type="session_prepare_intended",
                payload={"sessionMode": execution.session_mode},
            )
            session = await runtime.prepare_session(agent_request)
            await self._executions.record_session(
                request.execution_id,
                lease_owner=invocation.lease_owner,
                lease_epoch=lease_epoch,
                provider=execution.provider,
                native_session_id=session.provider_session_id,
                workspace_id=execution.workspace_id,
                lease_duration=self._lease_duration,
            )
            await self._record_evidence(
                request,
                invocation=invocation,
                event_type="session_prepared",
                payload={"providerSessionId": session.provider_session_id},
                provider_session_id=session.provider_session_id,
            )
            await self._record_evidence(
                request,
                invocation=invocation,
                event_type="turn_start_intended",
                payload={"providerSessionId": session.provider_session_id},
                provider_session_id=session.provider_session_id,
            )
            handle = await runtime.start_turn(agent_request, session)
            await self._executions.checkpoint(
                request.execution_id,
                lease_owner=invocation.lease_owner,
                lease_epoch=lease_epoch,
                status="running",
                sequence=0,
                native_operation_id=handle.provider_turn_id,
                attempt_id=invocation.attempt_id,
            )
            await self._record_evidence(
                request,
                invocation=invocation,
                event_type="turn_started",
                payload={"providerTurnId": handle.provider_turn_id},
                provider_session_id=handle.provider_session_id,
                provider_turn_id=handle.provider_turn_id,
            )
            heartbeat(
                AgentHeartbeat(
                    execution_id=request.execution_id,
                    lease_epoch=lease_epoch,
                    phase="running",
                    last_sequence=0,
                )
            )
            terminal, result_artifact_ref = await self._run_turn(
                runtime_name=execution.provider,
                handle=handle,
                invocation=invocation,
                lease_epoch=lease_epoch,
                heartbeat=heartbeat,
            )
            result = self._terminal_result(request, terminal)
            await self._record_evidence(
                request,
                invocation=invocation,
                event_type="provider_terminal_observed",
                payload={
                    "kind": terminal.kind,
                    "sequence": terminal.sequence,
                    "resultArtifactId": result_artifact_ref,
                },
                provider_session_id=terminal.provider_session_id,
                provider_turn_id=handle.provider_turn_id,
            )
            if result.outcome == "succeeded":
                await self._record_evidence(
                    request,
                    invocation=invocation,
                    event_type="output_validated",
                    payload={"schemaSha256": request.output_schema.sha256},
                    provider_session_id=terminal.provider_session_id,
                    provider_turn_id=handle.provider_turn_id,
                )
            terminal_status: Literal["succeeded", "failed", "cancelled"]
            if result.outcome == "succeeded":
                terminal_status = "succeeded"
            elif result.outcome == "failed":
                terminal_status = "failed"
            else:
                terminal_status = "cancelled"
            await self._executions.finalize(
                request.execution_id,
                lease_owner=invocation.lease_owner,
                lease_epoch=lease_epoch,
                status=terminal_status,
                result_payload=result.output,
                result_artifact_ref=result_artifact_ref,
                error_code=result.error_code,
                error_message=result.error_message,
                attempt_id=invocation.attempt_id,
            )
            preserve_workspace = False
            return result
        except asyncio.CancelledError:
            if handle is not None:
                await runtime.cancel(handle)
            if start_intent_durable:
                await self._record_evidence(
                    request,
                    invocation=invocation,
                    event_type="cancellation_requested",
                    payload={"confirmed": False},
                    provider_session_id=(
                        handle.provider_session_id if handle is not None else None
                    ),
                    provider_turn_id=handle.provider_turn_id if handle is not None else None,
                )
                await self._mark_attention(
                    request,
                    invocation,
                    lease_epoch,
                    "activity cancellation was not confirmed by the provider",
                )
            raise
        except Exception as exc:
            if not start_intent_durable:
                raise self._application_error(exc) from exc
            reason = self._attention_reason(exc)
            await self._record_evidence(
                request,
                invocation=invocation,
                event_type="reconciliation_required",
                payload={
                    "errorCode": getattr(exc, "code", "agent.execution_uncertain"),
                    "reason": reason,
                },
                provider_session_id=(handle.provider_session_id if handle is not None else None),
                provider_turn_id=handle.provider_turn_id if handle is not None else None,
            )
            await self._mark_attention(request, invocation, lease_epoch, reason)
            return NodeActivityResult(
                execution_id=request.execution_id,
                outcome="reconciliation_required",
                error_code=getattr(exc, "code", "agent.execution_uncertain"),
                error_message=reason,
            )
        finally:
            if prepared is not None:
                await self._cleanup_workspace(
                    prepared,
                    invocation,
                    request=request,
                    preserve=preserve_workspace,
                )

    async def _run_turn(
        self,
        *,
        runtime_name: str,
        handle: AgentTurnHandle,
        invocation: AgentActivityInvocation,
        lease_epoch: int,
        heartbeat: Callable[[AgentHeartbeat], None],
    ) -> tuple[
        AgentCompletedEvent | AgentFailedEvent | AgentCancelledEvent,
        str | None,
    ]:
        runtime = self._runtimes.get(runtime_name)
        last_sequence = 0
        terminal_artifact_ref: str | None = None

        async def consume() -> AgentCompletedEvent | AgentFailedEvent | AgentCancelledEvent:
            nonlocal last_sequence, terminal_artifact_ref
            terminal: AgentCompletedEvent | AgentFailedEvent | AgentCancelledEvent | None = None
            async for event in validate_agent_stream(
                runtime.stream(handle),
                execution_id=handle.execution_id,
                provider_session_id=handle.provider_session_id,
            ):
                await self._executions.checkpoint(
                    handle.execution_id,
                    lease_owner=invocation.lease_owner,
                    lease_epoch=lease_epoch,
                    status="running",
                    sequence=event.sequence,
                    native_operation_id=handle.provider_turn_id,
                    attempt_id=invocation.attempt_id,
                )
                last_sequence = event.sequence
                artifact_ref = await self._record_provider_event(
                    event,
                    invocation=invocation,
                    provider=runtime_name,
                    provider_turn_id=handle.provider_turn_id,
                )
                if artifact_ref is not None and isinstance(
                    event,
                    (AgentCompletedEvent, AgentFailedEvent, AgentCancelledEvent),
                ):
                    terminal_artifact_ref = artifact_ref
                if isinstance(event, (AgentCompletedEvent, AgentFailedEvent, AgentCancelledEvent)):
                    terminal = event
            if terminal is None:
                raise AgentRuntimeError(
                    "agent stream did not provide a terminal event",
                    code="agent.stream_incomplete",
                    reconciliation_required=True,
                )
            return terminal

        async def renew() -> None:
            while True:
                await asyncio.sleep(self._heartbeat_interval.total_seconds())
                renewed = await self._executions.renew(
                    handle.execution_id,
                    lease_owner=invocation.lease_owner,
                    lease_epoch=lease_epoch,
                    lease_duration=self._lease_duration,
                )
                if not renewed:
                    raise ExecutionLeaseLost("agent execution lease was lost during the turn")
                heartbeat(
                    AgentHeartbeat(
                        execution_id=handle.execution_id,
                        lease_epoch=lease_epoch,
                        phase="running",
                        last_sequence=last_sequence,
                    )
                )

        stream_task = asyncio.create_task(consume())
        heartbeat_task = asyncio.create_task(renew())
        done, _ = await asyncio.wait(
            {stream_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat_task in done:
            error = heartbeat_task.exception()
            if error is not None:
                try:
                    await runtime.cancel(handle)
                finally:
                    stream_task.cancel()
                    await asyncio.gather(stream_task, return_exceptions=True)
                raise error
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        return await stream_task, terminal_artifact_ref

    async def _prepare_workspace(
        self,
        request: NodeActivityRequest,
        execution: AgentExecutionIr,
    ) -> PreparedWorkspace:
        if execution.access == "read_only":
            return await self._workspaces.prepare(
                execution.workspace_id,
                request.execution_id,
                "read_only",
            )
        planned = await self._workspaces.plan_write(
            execution.workspace_id,
            request.execution_id,
        )
        await self._worktrees.register_preparing(
            WorktreeRegistration(
                worktree_id=planned.worktree_id,
                execution_id=request.execution_id,
                workspace_id=planned.workspace_id,
                relative_path=planned.relative_path,
                base_commit=planned.base_commit,
            )
        )
        prepared = await self._workspaces.materialize(planned)
        await self._worktrees.mark_ready(request.execution_id)
        await self._worktrees.mark_in_use(request.execution_id)
        return prepared

    async def _cleanup_workspace(
        self,
        prepared: PreparedWorkspace,
        invocation: AgentActivityInvocation,
        *,
        request: NodeActivityRequest,
        preserve: bool,
    ) -> None:
        claim: WorktreeCleanupClaim | None = None
        try:
            if prepared.access == "read_only":
                await self._record_evidence_safely(
                    request,
                    invocation=invocation,
                    event_type="cleanup_started",
                    payload={"access": prepared.access},
                )
                await self._workspaces.cleanup(prepared)
                await self._record_evidence_safely(
                    request,
                    invocation=invocation,
                    event_type="cleanup_completed",
                    payload={"disposition": "read_only_released"},
                )
                return
            claim = await self._worktrees.claim_cleanup(
                prepared.execution_id,
                cleanup_owner=invocation.lease_owner,
                lease_duration=self._cleanup_lease_duration,
            )
            if claim.disposition is not CleanupClaimDisposition.ACQUIRED:
                return
            await self._record_evidence_safely(
                request,
                invocation=invocation,
                event_type="cleanup_started",
                payload={"access": prepared.access, "cleanupEpoch": claim.cleanup_epoch},
            )
            cleanup = await self._workspaces.cleanup(prepared, preserve=preserve)
            disposition = "removed" if cleanup.disposition == "removed" else "preserved"
            await self._worktrees.finish_cleanup(
                prepared.execution_id,
                cleanup_owner=invocation.lease_owner,
                cleanup_epoch=claim.cleanup_epoch,
                disposition=disposition,
            )
            await self._record_evidence_safely(
                request,
                invocation=invocation,
                event_type=(
                    "cleanup_completed" if disposition == "removed" else "cleanup_preserved"
                ),
                payload={"disposition": disposition, "reason": cleanup.reason},
            )
        except Exception as exc:
            if prepared.access == "workspace_write" and claim is not None:
                try:
                    await self._worktrees.finish_cleanup(
                        prepared.execution_id,
                        cleanup_owner=invocation.lease_owner,
                        cleanup_epoch=claim.cleanup_epoch,
                        disposition="failed",
                        error=str(exc),
                    )
                except Exception:
                    pass
            await self._record_evidence_safely(
                request,
                invocation=invocation,
                event_type="cleanup_failed",
                payload={"errorType": type(exc).__name__},
            )

    async def _mark_attention(
        self,
        request: NodeActivityRequest,
        invocation: AgentActivityInvocation,
        lease_epoch: int,
        reason: str,
    ) -> None:
        try:
            await self._executions.finalize(
                request.execution_id,
                lease_owner=invocation.lease_owner,
                lease_epoch=lease_epoch,
                status="reconciliation_required",
                error_code="agent.reconciliation_required",
                error_message=reason,
                reconcile_reason=reason,
                attempt_id=invocation.attempt_id,
            )
        except ExecutionLeaseLost:
            return

    async def _resolve_reconciliation(
        self,
        request: NodeActivityRequest,
        result: ReconcileResult,
    ) -> NodeActivityResult:
        if result.status == "succeeded":
            assert result.output is not None
            node_result = successful_activity_result(request, result.output)
            await self._executions.resolve_reconciliation(
                request.execution_id,
                status="succeeded",
                result_payload=result.output,
            )
            return node_result
        if result.status == "failed":
            assert result.error is not None
            await self._executions.resolve_reconciliation(
                request.execution_id,
                status="failed",
                error_code=result.error.code,
                error_message=result.error.message,
            )
            return NodeActivityResult(
                execution_id=request.execution_id,
                outcome="failed",
                error_code=result.error.code,
                error_message=result.error.message,
            )
        if result.status == "cancelled":
            await self._executions.resolve_reconciliation(
                request.execution_id,
                status="cancelled",
            )
            return NodeActivityResult(execution_id=request.execution_id, outcome="cancelled")
        return NodeActivityResult(
            execution_id=request.execution_id,
            outcome="reconciliation_required",
            error_code="agent.reconciliation_required",
            error_message="provider state could not be proven terminal",
        )

    async def _record_provider_event(
        self,
        event: AgentEvent,
        *,
        invocation: AgentActivityInvocation,
        provider: str,
        provider_turn_id: str,
    ) -> str | None:
        if event.kind == "message_delta":
            return None
        payload = cast(JsonObject, event.model_dump(mode="json"))
        record = await self._evidence.record(
            execution_id=event.execution_id,
            attempt_id=invocation.attempt_id,
            event_type="provider_event_observed",
            provider=provider,
            payload=payload,
            provider_session_id=event.provider_session_id,
            provider_turn_id=provider_turn_id,
            preserve_payload=True,
        )
        artifact_id = record.payload.get("rawArtifactId")
        if not isinstance(artifact_id, str):
            return None
        await self._evidence.record(
            execution_id=event.execution_id,
            attempt_id=invocation.attempt_id,
            event_type="artifact_committed",
            provider=provider,
            payload={
                "artifactId": artifact_id,
                "providerEventKind": event.kind,
                "providerSequence": event.sequence,
            },
            provider_session_id=event.provider_session_id,
            provider_turn_id=provider_turn_id,
        )
        return artifact_id

    async def _record_evidence(
        self,
        request: NodeActivityRequest,
        *,
        invocation: AgentActivityInvocation | None,
        event_type: str,
        payload: JsonObject,
        provider_session_id: str | None = None,
        provider_turn_id: str | None = None,
    ) -> None:
        execution = cast(AgentExecutionIr, request.execution)
        await self._evidence.record(
            execution_id=request.execution_id,
            attempt_id=invocation.attempt_id if invocation is not None else None,
            event_type=event_type,
            provider=execution.provider,
            payload=payload,
            provider_session_id=provider_session_id,
            provider_turn_id=provider_turn_id,
        )

    async def _record_evidence_safely(
        self,
        request: NodeActivityRequest,
        *,
        invocation: AgentActivityInvocation | None,
        event_type: str,
        payload: JsonObject,
        provider_session_id: str | None = None,
        provider_turn_id: str | None = None,
    ) -> None:
        try:
            await self._record_evidence(
                request,
                invocation=invocation,
                event_type=event_type,
                payload=payload,
                provider_session_id=provider_session_id,
                provider_turn_id=provider_turn_id,
            )
        except Exception:
            return

    @staticmethod
    def _registration(
        request: NodeActivityRequest,
        execution: AgentExecutionIr,
    ) -> ExecutionRegistration:
        payload = request.model_dump(mode="json")
        request_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ExecutionRegistration(
            execution_id=request.execution_id,
            idempotency_key=request.idempotency_key,
            workflow_instance_id=request.workflow_instance_id,
            node_id=request.node_id,
            activation=request.activation,
            plan_hash=request.plan_hash,
            request_hash=request_hash,
            output_schema_hash=request.output_schema.sha256,
            provider=execution.provider,
            model=execution.model,
            effort=execution.effort,
            workspace_id=execution.workspace_id,
            access_mode=execution.access,
            session_mode=execution.session_mode,
        )

    @staticmethod
    def _agent_request(
        request: NodeActivityRequest,
        execution: AgentExecutionIr,
        prepared: PreparedWorkspace,
        attempt: int,
    ) -> AgentStartRequest | AgentResumeRequest:
        identity = AgentExecutionIdentity(
            execution_id=request.execution_id,
            workflow_instance_id=request.workflow_instance_id,
            node_id=request.node_id,
            activation=request.activation,
            attempt=attempt,
            idempotency_key=request.idempotency_key,
        )
        workspace = WorkspaceLease(
            lease_id=request.execution_id,
            workspace_id=execution.workspace_id,
            root=prepared.path,
            access_mode=execution.access,
            isolated=prepared.owns_worktree,
            worktree_id=(
                hashlib.sha256(request.execution_id.encode()).hexdigest()[:24]
                if prepared.owns_worktree
                else None
            ),
        )
        policy = AgentPolicyContext(
            sandbox_mode=execution.access,
            approval_mode=execution.approval_mode,
            network_policy=execution.network_policy,
            allowed_tool_profile=execution.allowed_tool_profile,
            sandbox_requirements=SandboxRequirements(
                filesystem="full" if execution.access == "read_only" else "partial",
                network="full" if execution.network_policy == "deny" else "unavailable",
            ),
        )
        prompt = (
            f"{execution.instruction.rstrip()}\n\n"
            "Resolved task inputs (JSON):\n"
            f"{json.dumps(request.resolved_inputs, sort_keys=True, ensure_ascii=False)}"
        )
        common: dict[str, object] = {
            "identity": identity,
            "provider": execution.provider,
            "model": execution.model,
            "effort": execution.effort,
            "workspace": workspace,
            "prompt": prompt,
            "resolved_inputs": request.resolved_inputs,
            "output_schema": request.output_schema,
            "timeout_ms": execution.timeout_ms,
            "policy": policy,
        }
        if execution.session_mode == "resume":
            assert request.provider_session_id is not None
            return AgentResumeRequest(
                **common,  # pyright: ignore[reportArgumentType]
                provider_session_id=request.provider_session_id,
            )
        return AgentStartRequest(**common)  # pyright: ignore[reportArgumentType]

    @staticmethod
    def _terminal_result(
        request: NodeActivityRequest,
        terminal: AgentCompletedEvent | AgentFailedEvent | AgentCancelledEvent,
    ) -> NodeActivityResult:
        if isinstance(terminal, AgentCompletedEvent):
            return successful_activity_result(request, terminal.output)
        if isinstance(terminal, AgentFailedEvent):
            return NodeActivityResult(
                execution_id=request.execution_id,
                outcome="failed",
                error_code=terminal.error.code,
                error_message=terminal.error.message,
            )
        return NodeActivityResult(execution_id=request.execution_id, outcome="cancelled")

    @staticmethod
    def _cached_result(
        request: NodeActivityRequest,
        status: str,
        payload: Mapping[str, object] | None,
        error_code: str | None,
    ) -> NodeActivityResult:
        if status == "succeeded" and payload is not None:
            return successful_activity_result(request, cast(JsonObject, dict(payload)))
        outcome: Literal["failed", "timed_out", "cancelled"]
        if status == "timed_out":
            outcome = "timed_out"
        elif status == "cancelled":
            outcome = "cancelled"
        else:
            outcome = "failed"
        return NodeActivityResult(
            execution_id=request.execution_id,
            outcome=outcome,
            error_code=error_code,
        )

    @staticmethod
    def _attention_reason(exc: Exception) -> str:
        if isinstance(exc, AgentRuntimeError) and exc.reconciliation_required:
            return str(exc)
        return f"external Agent state is uncertain after {type(exc).__name__}"

    @staticmethod
    def _application_error(exc: Exception) -> ApplicationError:
        if isinstance(exc, AgentRuntimeError):
            return ApplicationError(
                str(exc),
                type=exc.code,
                non_retryable=not exc.retryable,
            )
        return ApplicationError(str(exc), type=type(exc).__name__)


class TemporalAgentActivities:
    def __init__(self, runner: AgentActivityRunner, *, worker_id: str) -> None:
        self._runner = runner
        self._worker_id = worker_id

    @activity.defn(name="agent.execute.v1")
    async def execute(self, request: NodeActivityRequest) -> NodeActivityResult:
        info = activity.info()
        if info.workflow_run_id is None:
            raise ApplicationError(
                "Temporal did not provide a workflow run ID",
                type="AgentActivityContextInvalid",
                non_retryable=True,
            )
        return await self._runner.execute(
            request,
            AgentActivityInvocation(
                workflow_run_id=info.workflow_run_id,
                activity_id=info.activity_id,
                attempt=info.attempt,
                worker_id=self._worker_id,
            ),
            heartbeat=lambda detail: activity.heartbeat(detail.model_dump(mode="json")),
        )
