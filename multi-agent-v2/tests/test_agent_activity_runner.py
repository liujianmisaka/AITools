from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from multi_agent_v2.packages.agent_execution import (
    AgentActivityInvocation,
    AgentActivityRunner,
    AgentHeartbeat,
)
from multi_agent_v2.packages.agent_runtime import (
    AgentExecutionRequest,
    AgentRuntimeRegistry,
    FakeRuntime,
    FakeScenario,
    PreparedAgentSession,
    ReconcileResult,
)
from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.persistence import (
    CleanupClaimDisposition,
    EvidenceEventRecord,
    ExecutionAttemptRegistration,
    ExecutionLeaseRepository,
    ExecutionRegistration,
    LeaseClaimDisposition,
    LeaseClaimResult,
    ProviderSessionClaim,
    WorktreeCleanupClaim,
    WorktreeRepository,
)
from multi_agent_v2.packages.policy import (
    PlannedWorktree,
    PreparedWorkspace,
    WorkspaceCleanupResult,
    WorkspaceDefinition,
    WorkspaceRegistry,
    WorkspaceSupervisor,
)
from multi_agent_v2.packages.workflow_dsl.ir import (
    AgentExecutionIr,
    RetryIr,
    StrictSchemaIr,
)
from multi_agent_v2.packages.workflow_runtime.messages import NodeActivityRequest


class _ExecutionRepositoryStub:
    def __init__(
        self,
        claim: LeaseClaimResult,
        *,
        renew_results: list[bool] | None = None,
    ) -> None:
        self.claim_result = claim
        self.renew_results = renew_results or [True]
        self.events: list[str] = []
        self.checkpoints: list[int] = []
        self.finalizations: list[tuple[str, Mapping[str, object] | None]] = []
        self.result_artifact_refs: list[str | None] = []
        self.reconciliation_resolutions: list[str] = []

    async def claim(
        self,
        registration: ExecutionRegistration,
        *,
        lease_owner: str,
        lease_duration: timedelta,
    ) -> LeaseClaimResult:
        del registration, lease_owner, lease_duration
        self.events.append("claim")
        return self.claim_result

    async def begin_attempt(
        self,
        registration: ExecutionAttemptRegistration,
        *,
        lease_owner: str,
        lease_epoch: int,
    ) -> None:
        del registration, lease_owner, lease_epoch
        self.events.append("begin_attempt")

    async def mark_start_intent(
        self,
        execution_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
    ) -> object:
        del execution_id, lease_owner, lease_epoch
        self.events.append("start_intent")
        return object()

    async def record_session(
        self,
        execution_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        provider: str,
        native_session_id: str,
        workspace_id: str,
        lease_duration: timedelta,
    ) -> ProviderSessionClaim:
        del (
            execution_id,
            lease_owner,
            lease_epoch,
            provider,
            native_session_id,
            workspace_id,
            lease_duration,
        )
        self.events.append("record_session")
        return ProviderSessionClaim(session_key="a" * 64, claim_epoch=1)

    async def checkpoint(
        self,
        execution_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        status: str,
        sequence: int,
        native_operation_id: str | None = None,
        process_id: int | None = None,
        process_started_at: object | None = None,
        attempt_id: str | None = None,
    ) -> None:
        del (
            execution_id,
            lease_owner,
            lease_epoch,
            status,
            native_operation_id,
            process_id,
            process_started_at,
            attempt_id,
        )
        self.events.append("checkpoint")
        self.checkpoints.append(sequence)

    async def renew(
        self,
        execution_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        lease_duration: timedelta,
    ) -> bool:
        del execution_id, lease_owner, lease_epoch, lease_duration
        self.events.append("renew")
        if len(self.renew_results) > 1:
            return self.renew_results.pop(0)
        return self.renew_results[0]

    async def finalize(
        self,
        execution_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        status: str,
        result_payload: Mapping[str, object] | None = None,
        result_artifact_ref: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        reconcile_reason: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        del (
            execution_id,
            lease_owner,
            lease_epoch,
            error_code,
            error_message,
            reconcile_reason,
            attempt_id,
        )
        self.events.append(f"finalize:{status}")
        self.finalizations.append((status, result_payload))
        self.result_artifact_refs.append(result_artifact_ref)

    async def resolve_reconciliation(
        self,
        execution_id: str,
        *,
        status: str,
        result_payload: Mapping[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        del execution_id, result_payload, error_code, error_message
        self.reconciliation_resolutions.append(status)


class _EvidenceRecorderStub:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def record(
        self,
        *,
        execution_id: str,
        attempt_id: str | None,
        event_type: str,
        provider: str,
        payload: JsonObject,
        provider_session_id: str | None = None,
        provider_turn_id: str | None = None,
        preserve_payload: bool = False,
    ) -> EvidenceEventRecord:
        del preserve_payload
        self.events.append(event_type)
        stored_payload = dict(payload)
        if event_type == "provider_event_observed":
            stored_payload["rawArtifactId"] = f"artifact-{len(self.events)}"
        return EvidenceEventRecord(
            event_id=f"event-{len(self.events)}",
            execution_id=execution_id,
            attempt_id=attempt_id,
            sequence=len(self.events),
            event_type=event_type,
            provider=provider,
            payload=stored_payload,
            provider_session_id=provider_session_id,
            provider_turn_id=provider_turn_id,
            occurred_at=datetime.now(UTC),
        )


class _WorktreeRepositoryStub:
    def __init__(self) -> None:
        self.finished: list[str] = []

    async def register_preparing(self, registration: object) -> None:
        del registration

    async def mark_ready(self, execution_id: str) -> None:
        del execution_id

    async def mark_in_use(self, execution_id: str) -> None:
        del execution_id

    async def claim_cleanup(
        self,
        execution_id: str,
        *,
        cleanup_owner: str,
        lease_duration: timedelta,
    ) -> WorktreeCleanupClaim:
        del cleanup_owner, lease_duration
        return WorktreeCleanupClaim(
            disposition=CleanupClaimDisposition.ACQUIRED,
            execution_id=execution_id,
            state="cleanup_pending",
            cleanup_epoch=1,
        )

    async def finish_cleanup(
        self,
        execution_id: str,
        *,
        cleanup_owner: str,
        cleanup_epoch: int,
        disposition: str,
        error: str | None = None,
    ) -> None:
        del execution_id, cleanup_owner, cleanup_epoch, error
        self.finished.append(disposition)


class _WorkspaceLifecycleStub:
    def __init__(self, root: Path) -> None:
        self._root = root
        self.preserve_flags: list[bool] = []

    async def plan_write(self, workspace_id: str, execution_id: str) -> PlannedWorktree:
        return PlannedWorktree(
            worktree_id="worktree-1",
            workspace_id=workspace_id,
            execution_id=execution_id,
            target_path=self._root / "worktree-1",
            relative_path="worktree-1",
            base_commit="a" * 40,
        )

    async def materialize(self, planned: PlannedWorktree) -> PreparedWorkspace:
        planned.target_path.mkdir(parents=True, exist_ok=True)
        return PreparedWorkspace(
            workspace_id=planned.workspace_id,
            execution_id=planned.execution_id,
            access="workspace_write",
            path=planned.target_path,
            repository_root=self._root,
            base_commit=planned.base_commit,
            owns_worktree=True,
            reconciled=False,
        )

    async def cleanup(
        self,
        prepared: PreparedWorkspace,
        *,
        preserve: bool = False,
    ) -> WorkspaceCleanupResult:
        self.preserve_flags.append(preserve)
        return WorkspaceCleanupResult(
            disposition="preserved" if preserve else "removed",
            path=prepared.path,
            reason="test",
        )


class _LoggedFakeRuntime(FakeRuntime):
    def __init__(
        self,
        events: list[str],
        *,
        scenarios: Mapping[str, FakeScenario] | None = None,
        reconcile_overrides: Mapping[str, ReconcileResult] | None = None,
    ) -> None:
        super().__init__(
            scenarios=scenarios,
            reconcile_overrides=reconcile_overrides,
        )
        self._activity_events = events

    async def prepare_session(
        self,
        request: AgentExecutionRequest,
    ) -> PreparedAgentSession:
        self._activity_events.append("prepare_session")
        return await super().prepare_session(request)


def _schema() -> StrictSchemaIr:
    canonical = json.dumps(
        {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return StrictSchemaIr(
        canonical=canonical,
        sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def _request(access: str = "read_only") -> NodeActivityRequest:
    return NodeActivityRequest(
        workflow_instance_id="workflow-1",
        plan_hash="a" * 64,
        node_id="agent",
        activation=1,
        execution_id="workflow-1:agent:1",
        idempotency_key="workflow-1:agent:1",
        resolved_inputs={"question": "test"},
        provider_session_id=None,
        execution=AgentExecutionIr(
            provider="fake",
            model="fake/model",
            effort="high",
            workspace_id="repo",
            access=access,  # type: ignore[arg-type]
            approval_mode="deny_all",
            network_policy="deny",
            allowed_tool_profile="coding-default",
            session_mode="new",
            provider_session_expression=None,
            instruction="Return the answer.",
            timeout_ms=30_000,
            retry=RetryIr(
                maximum_attempts=1,
                initial_interval_ms=100,
                maximum_interval_ms=100,
            ),
        ),
        output_schema=_schema(),
    )


def _workspace_supervisor(tmp_path: Path) -> WorkspaceSupervisor:
    repository = tmp_path / "repository"
    worktree_root = tmp_path / "worktrees"
    repository.mkdir()
    worktree_root.mkdir()
    return WorkspaceSupervisor(
        WorkspaceRegistry(
            [
                WorkspaceDefinition(
                    workspace_id="repo",
                    root=repository,
                    worktree_root=worktree_root,
                )
            ]
        )
    )


def _invocation() -> AgentActivityInvocation:
    return AgentActivityInvocation(
        workflow_run_id="run-1",
        activity_id="agent:1",
        attempt=1,
        worker_id="worker-1",
    )


async def test_activity_persists_start_intent_before_provider_session(tmp_path: Path) -> None:
    request = _request()
    repository = _ExecutionRepositoryStub(
        LeaseClaimResult(
            disposition=LeaseClaimDisposition.ACQUIRED,
            execution_id=request.execution_id,
            status="leased",
            lease_epoch=1,
        )
    )
    runtime = _LoggedFakeRuntime(
        repository.events,
        scenarios={
            request.execution_id: FakeScenario(output={"answer": "done"}),
        },
    )
    evidence = _EvidenceRecorderStub()
    evidence = _EvidenceRecorderStub()
    runner = AgentActivityRunner(
        executions=cast(ExecutionLeaseRepository, repository),
        worktrees=cast(WorktreeRepository, object()),
        workspaces=_workspace_supervisor(tmp_path),
        runtimes=AgentRuntimeRegistry([runtime]),
        evidence=evidence,
    )
    heartbeats: list[AgentHeartbeat] = []

    result = await runner.execute(request, _invocation(), heartbeat=heartbeats.append)

    assert result.outcome == "succeeded"
    assert result.output == {"answer": "done"}
    assert repository.events.index("start_intent") < repository.events.index("prepare_session")
    assert repository.events.index("prepare_session") < repository.events.index("record_session")
    assert repository.checkpoints == [0, 1, 2]
    assert repository.finalizations == [("succeeded", {"answer": "done"})]
    assert repository.result_artifact_refs[-1] is not None
    assert evidence.events.count("artifact_committed") == 2
    assert "provider_terminal_observed" in evidence.events
    assert "output_validated" in evidence.events
    assert heartbeats[0].phase == "running"


async def test_activity_lost_lease_becomes_reconciliation_required(tmp_path: Path) -> None:
    request = _request()
    repository = _ExecutionRepositoryStub(
        LeaseClaimResult(
            disposition=LeaseClaimDisposition.ACQUIRED,
            execution_id=request.execution_id,
            status="leased",
            lease_epoch=1,
        ),
        renew_results=[False],
    )
    runtime = _LoggedFakeRuntime(
        repository.events,
        scenarios={
            request.execution_id: FakeScenario(
                delay_seconds=0.2,
                output={"answer": "too late"},
            ),
        },
    )
    evidence = _EvidenceRecorderStub()
    runner = AgentActivityRunner(
        executions=cast(ExecutionLeaseRepository, repository),
        worktrees=cast(WorktreeRepository, object()),
        workspaces=_workspace_supervisor(tmp_path),
        runtimes=AgentRuntimeRegistry([runtime]),
        evidence=evidence,
        lease_duration=timedelta(milliseconds=100),
        heartbeat_interval=timedelta(milliseconds=10),
    )

    result = await runner.execute(request, _invocation(), heartbeat=lambda _detail: None)

    assert result.outcome == "reconciliation_required"
    assert runtime.cancel_count == 1
    assert repository.finalizations[-1][0] == "reconciliation_required"


async def test_activity_resolves_a_cached_provider_terminal_state(tmp_path: Path) -> None:
    request = _request()
    repository = _ExecutionRepositoryStub(
        LeaseClaimResult(
            disposition=LeaseClaimDisposition.RECONCILIATION_REQUIRED,
            execution_id=request.execution_id,
            status="reconciliation_required",
            lease_epoch=2,
            provider_session_id="fake-session",
            provider_turn_id="fake-turn",
            last_sequence=2,
        )
    )
    runtime = _LoggedFakeRuntime(
        repository.events,
        reconcile_overrides={
            request.execution_id: ReconcileResult(
                execution_id=request.execution_id,
                status="succeeded",
                provider_session_id="fake-session",
                provider_turn_id="fake-turn",
                last_sequence=2,
                output={"answer": "recovered"},
            )
        },
    )
    evidence = _EvidenceRecorderStub()
    runner = AgentActivityRunner(
        executions=cast(ExecutionLeaseRepository, repository),
        worktrees=cast(WorktreeRepository, object()),
        workspaces=_workspace_supervisor(tmp_path),
        runtimes=AgentRuntimeRegistry([runtime]),
        evidence=evidence,
    )

    result = await runner.execute(request, _invocation(), heartbeat=lambda _detail: None)

    assert result.outcome == "succeeded"
    assert result.output == {"answer": "recovered"}
    assert repository.reconciliation_resolutions == ["succeeded"]
    assert evidence.events == ["reconciliation_required"]


async def test_terminal_write_execution_only_preserves_a_changed_worktree(
    tmp_path: Path,
) -> None:
    request = _request("workspace_write")
    repository = _ExecutionRepositoryStub(
        LeaseClaimResult(
            disposition=LeaseClaimDisposition.ACQUIRED,
            execution_id=request.execution_id,
            status="leased",
            lease_epoch=1,
        )
    )
    runtime = _LoggedFakeRuntime(
        repository.events,
        scenarios={request.execution_id: FakeScenario(output={"answer": "done"})},
    )
    worktrees = _WorktreeRepositoryStub()
    workspaces = _WorkspaceLifecycleStub(tmp_path)
    runner = AgentActivityRunner(
        executions=cast(ExecutionLeaseRepository, repository),
        worktrees=cast(WorktreeRepository, worktrees),
        workspaces=cast(WorkspaceSupervisor, workspaces),
        runtimes=AgentRuntimeRegistry([runtime]),
        evidence=_EvidenceRecorderStub(),
    )

    result = await runner.execute(request, _invocation(), heartbeat=lambda _detail: None)

    assert result.outcome == "succeeded"
    assert workspaces.preserve_flags == [False]
    assert worktrees.finished == ["removed"]


async def test_uncertain_write_execution_preserves_its_worktree(tmp_path: Path) -> None:
    request = _request("workspace_write")
    repository = _ExecutionRepositoryStub(
        LeaseClaimResult(
            disposition=LeaseClaimDisposition.ACQUIRED,
            execution_id=request.execution_id,
            status="leased",
            lease_epoch=1,
        ),
        renew_results=[False],
    )
    runtime = _LoggedFakeRuntime(
        repository.events,
        scenarios={
            request.execution_id: FakeScenario(
                delay_seconds=0.2,
                output={"answer": "too late"},
            )
        },
    )
    worktrees = _WorktreeRepositoryStub()
    workspaces = _WorkspaceLifecycleStub(tmp_path)
    runner = AgentActivityRunner(
        executions=cast(ExecutionLeaseRepository, repository),
        worktrees=cast(WorktreeRepository, worktrees),
        workspaces=cast(WorkspaceSupervisor, workspaces),
        runtimes=AgentRuntimeRegistry([runtime]),
        evidence=_EvidenceRecorderStub(),
        lease_duration=timedelta(milliseconds=100),
        heartbeat_interval=timedelta(milliseconds=10),
    )

    result = await runner.execute(request, _invocation(), heartbeat=lambda _detail: None)

    assert result.outcome == "reconciliation_required"
    assert workspaces.preserve_flags == [True]
    assert worktrees.finished == ["preserved"]
