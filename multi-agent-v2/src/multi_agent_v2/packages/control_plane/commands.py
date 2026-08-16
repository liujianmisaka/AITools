from __future__ import annotations

from collections.abc import Mapping

from multi_agent_v2.packages.control_plane.models import (
    ApprovalDecision,
    CommandAccepted,
    WorkflowSignal,
    WorkflowUpdate,
)
from multi_agent_v2.packages.persistence import ControlPlaneConflict, ControlPlaneRepository
from multi_agent_v2.packages.workflow_runtime.messages import ApprovalCommand, CommandResult
from multi_agent_v2.packages.workflow_runtime.temporal import TemporalGateway


class WorkflowCommandService:
    def __init__(
        self,
        *,
        repository: ControlPlaneRepository,
        temporal: TemporalGateway,
    ) -> None:
        self._repository = repository
        self._temporal = temporal

    async def decide_approval(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        command_id: str,
    ) -> CommandAccepted:
        approval = await self._repository.get_approval(approval_id)
        if approval.status != "pending":
            if approval.command_id == command_id:
                return CommandAccepted(command_id=command_id, accepted=False)
            raise ControlPlaneConflict("approval is no longer pending")
        instance = await self._repository.get_instance(approval.instance_id)
        client = await self._temporal.connect()
        handle = client.get_workflow_handle(instance.temporal_workflow_id)
        result = await handle.execute_update(  # pyright: ignore[reportUnknownMemberType]
            "approval.decide.v1",
            ApprovalCommand(
                command_id=command_id,
                node_id=approval.node_id,
                activation=approval.activation,
                decision=decision.decision,
                operator_label=decision.operator_label,
                reason=decision.reason,
            ),
            id=command_id,
            result_type=CommandResult,
        )
        parsed = CommandResult.model_validate(result)
        return CommandAccepted(command_id=command_id, accepted=parsed.accepted)

    async def cancel_instance(
        self,
        instance_id: str,
        *,
        command_id: str,
        reason: str,
    ) -> CommandAccepted:
        instance = await self._repository.get_instance(instance_id)
        client = await self._temporal.connect()
        handle = client.get_workflow_handle(instance.temporal_workflow_id)
        await handle.cancel(reason=reason)
        return CommandAccepted(command_id=command_id, accepted=True)

    async def signal_instance(
        self,
        instance_id: str,
        signal: WorkflowSignal,
        *,
        command_id: str,
    ) -> CommandAccepted:
        instance = await self._repository.get_instance(instance_id)
        client = await self._temporal.connect()
        handle = client.get_workflow_handle(instance.temporal_workflow_id)
        await handle.signal(
            signal.signal_name,
            _command_payload(signal.data, command_id),
        )
        return CommandAccepted(command_id=command_id, accepted=True)

    async def update_instance(
        self,
        instance_id: str,
        update_name: str,
        update: WorkflowUpdate,
        *,
        command_id: str,
    ) -> CommandAccepted:
        instance = await self._repository.get_instance(instance_id)
        client = await self._temporal.connect()
        handle = client.get_workflow_handle(instance.temporal_workflow_id)
        await handle.execute_update(  # pyright: ignore[reportUnknownMemberType]
            update_name,
            _command_payload(update.data, command_id),
            id=command_id,
        )
        return CommandAccepted(command_id=command_id, accepted=True)


def _command_payload(data: Mapping[str, object], command_id: str) -> dict[str, object]:
    payload = dict(data)
    key = "commandId" if "commandId" in payload else "command_id"
    existing = payload.get(key)
    if existing is not None and existing != command_id:
        raise ControlPlaneConflict("command payload ID does not match Idempotency-Key")
    payload[key] = command_id
    return payload
