from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mcp.server.fastmcp import FastMCP

from misaka_coordinator_service.application import (
    CoordinatorActivationRequest,
    CoordinatorMessageResult,
)
from misaka_coordinator_service.execution import (
    DelegationSnapshot,
    MessageDelivery,
    ReconciliationStatus,
)
from misaka_coordinator_service.persistence import CoordinatorSessionRecord

if TYPE_CHECKING:
    from misaka_coordinator_service.transport.host import CoordinatorHostRuntime


def register_tools(server: FastMCP[Any], runtime: CoordinatorHostRuntime) -> None:
    @server.tool()
    async def coordinator_activate(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        prompt: str,
        cwd: str,
        cognitive_session_id: str | None = None,
        acceptance_criteria: list[str] | None = None,
        constraints: list[str] | None = None,
        activation_id: str | None = None,
    ) -> dict[str, object]:
        result = await runtime.service.activate(
            CoordinatorActivationRequest(
                session_id=session_id,
                prompt=prompt,
                cwd=cwd,
                cognitive_session_id=cognitive_session_id,
                acceptance_criteria=tuple(acceptance_criteria or ()),
                constraints=tuple(constraints or ()),
                activation_id=activation_id,
            )
        )
        return result.to_dict()

    @server.tool()
    async def coordinator_get_session(session_id: str) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        return _record_payload(runtime.service.get(session_id))

    @server.tool()
    async def coordinator_list_sessions() -> list[str]:  # pyright: ignore[reportUnusedFunction]
        return list(runtime.service.list_session_ids())

    @server.tool()
    async def coordinator_list_monitors() -> list[dict[str, object]]:  # pyright: ignore[reportUnusedFunction]
        return [status.to_dict() for status in runtime.service.monitor_statuses()]

    @server.tool()
    async def coordinator_list_tool_audits() -> list[dict[str, object]]:  # pyright: ignore[reportUnusedFunction]
        return list(runtime.tool_audits())

    @server.tool()
    async def coordinator_send_message(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        node_id: str,
        message: str,
        delivery: str = "append",
        expected_activation_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, object]:
        result = await runtime.service.send_message(
            session_id=session_id,
            node_id=node_id,
            message=message,
            delivery=MessageDelivery(delivery),
            expected_activation_id=expected_activation_id,
            model=model,
            effort=effort,
        )
        return _message_payload(result)

    @server.tool()
    async def coordinator_continue(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        node_id: str,
        message: str,
        expected_activation_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, object]:
        result = await runtime.service.continue_node(
            session_id=session_id,
            node_id=node_id,
            message=message,
            expected_activation_id=expected_activation_id,
            model=model,
            effort=effort,
        )
        return _message_payload(result)

    @server.tool()
    async def coordinator_cancel(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        node_id: str,
        reason: str,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        expected_activation_id: str | None = None,
    ) -> dict[str, object]:
        result = await runtime.service.cancel_node(
            session_id=session_id,
            node_id=node_id,
            reason=reason,
            request_id=request_id,
            idempotency_key=idempotency_key,
            expected_activation_id=expected_activation_id,
        )
        return {"session": result.session.to_dict(), "snapshot": _snapshot_payload(result.snapshot)}

    @server.tool()
    async def coordinator_reconcile(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        node_id: str,
        expected_revision: int,
        status: str,
        reason: str,
        output: object = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        result = await runtime.service.reconcile_node(
            session_id=session_id,
            node_id=node_id,
            expected_revision=expected_revision,
            status=ReconciliationStatus(status),
            reason=reason,
            output=cast(Any, output),
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        return {"session": result.session.to_dict(), "snapshot": _snapshot_payload(result.snapshot)}

    @server.tool()
    async def coordinator_resolve_approval(  # pyright: ignore[reportUnusedFunction]
        session_id: str,
        approval_id: str,
        approved: bool,
        actor_id: str,
        reason: str,
        expected_session_revision: int,
    ) -> dict[str, object]:
        result = await runtime.service.resolve_approval(
            session_id=session_id,
            approval_id=approval_id,
            approved=approved,
            actor_id=actor_id,
            reason=reason,
            expected_session_revision=expected_session_revision,
        )
        return result.to_dict()


def _record_payload(record: CoordinatorSessionRecord) -> dict[str, object]:
    return {
        "session": record.coordinator_session.to_dict(),
        "cognitive_session_id": record.agent_session.session_id,
        "working_directory": record.working_directory,
    }


def _message_payload(result: CoordinatorMessageResult) -> dict[str, object]:
    dispatch = result.dispatch
    return {
        "session": result.session.to_dict(),
        "dispatch": {
            "dispatch_id": dispatch.dispatch_id,
            "delegation_id": dispatch.delegation_id,
            "session_id": dispatch.session_id,
            "status": dispatch.status,
            "revision": dispatch.revision,
            "applied_strategy": dispatch.applied_strategy,
            "previous_activation_id": dispatch.previous_activation_id,
            "current_activation_id": dispatch.current_activation_id,
            "error_code": dispatch.error_code,
            "error_message": dispatch.error_message,
        },
    }


def _snapshot_payload(snapshot: DelegationSnapshot) -> dict[str, object]:
    return {
        "delegation_id": snapshot.delegation_id,
        "status": snapshot.status.value,
        "revision": snapshot.revision,
        "session_id": snapshot.session_id,
        "current_activation_id": snapshot.current_activation_id,
        "current_invocation_id": snapshot.current_invocation_id,
        "next_action": snapshot.next_action,
    }
