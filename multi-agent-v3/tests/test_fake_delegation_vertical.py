from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_control_plane import ControlPlaneService, WorkingDirectoryPolicy, create_app
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_invocation_runtime import InvocationRuntime

_CONTROLLER = {"principal_id": "vertical-controller", "kind": "application"}
_OBSERVER = {"principal_id": "vertical-observer", "kind": "human"}
_APPROVER = {"principal_id": "vertical-approver", "kind": "human"}


def _delegation_payload(
    delegation_id: str,
    provider_id: str,
    plan_hash: str,
    *,
    cwd: Path,
    scope: dict[str, str],
    child_scope: dict[str, str],
    parent_delegation_id: str | None = None,
) -> dict[str, Any]:
    return {
        "actor": _CONTROLLER,
        "delegation_id": delegation_id,
        "idempotency_key": f"{delegation_id}-idempotency",
        "initiator": _CONTROLLER,
        "controller": _CONTROLLER,
        "scope": scope,
        "capability_id": AGENT_CAPABILITY_ID,
        "operation": AGENT_OPERATION_INVOKE,
        "input": {"prompt": f"execute {delegation_id}"},
        "cwd": str(cwd),
        "provider_id": provider_id,
        "model": "fake/model",
        "effort": "high",
        "policy_context": {
            "sandbox": "read_only",
            "network_policy": "deny",
        },
        "output_schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        "plan_hash": plan_hash,
        "decision_ref": {
            "proposal_id": f"decision-{delegation_id}",
            "revision": 1,
        },
        "mode": "continuable",
        "parent_delegation_id": parent_delegation_id,
        "observers": [_OBSERVER],
        "policy": {
            "child_scope": child_scope,
            "budget": {
                "max_depth": 2,
                "fan_out_limit": 2,
                "max_concurrent_children": 2,
                "max_activations": 3,
            },
            "requested_effects": ["workspace.read"],
            "require_decision": True,
        },
    }


async def _approve_and_create(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    provider: FakeAgentProvider,
) -> dict[str, Any]:
    delegation_id = str(payload["delegation_id"])
    blocked = await client.post("/delegations", json=payload)
    assert blocked.status_code == 409
    assert "is pending" in blocked.json()["detail"]
    assert provider.starts == 0

    missing = await client.get(
        f"/delegations/{delegation_id}",
        params={"actor_id": _CONTROLLER["principal_id"], "actor_kind": "application"},
    )
    assert missing.status_code == 404

    approved = await client.post(
        f"/delegations/{delegation_id}/approve",
        json={
            "actor": _APPROVER,
            "decision_ref": payload["decision_ref"],
            "plan_hash": payload["plan_hash"],
            "reason": "approved by vertical acceptance",
        },
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    created = await client.post("/delegations", json=payload)
    assert created.status_code == 202
    await asyncio.wait_for(provider.started.wait(), timeout=1.0)

    repeated = await client.post("/delegations", json=payload)
    assert repeated.status_code == 202, repeated.json()
    assert repeated.json()["delegation_id"] == delegation_id
    assert provider.starts == 1
    return created.json()


async def _wait_terminal(
    client: httpx.AsyncClient,
    delegation_id: str,
    *,
    activation_count: int = 1,
) -> dict[str, Any]:
    for _ in range(200):
        response = await client.get(
            f"/delegations/{delegation_id}",
            params={"actor_id": _CONTROLLER["principal_id"], "actor_kind": "application"},
        )
        assert response.status_code == 200
        snapshot = response.json()
        if snapshot["report"] is not None and snapshot["activation_count"] >= activation_count:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"delegation {delegation_id} did not become terminal")


async def _wait_messages_completed(
    client: httpx.AsyncClient,
    delegation_id: str,
    message_ids: set[str],
) -> list[dict[str, Any]]:
    for _ in range(200):
        response = await client.get(
            f"/delegations/{delegation_id}/events",
            params={
                "actor_id": _OBSERVER["principal_id"],
                "actor_kind": "human",
            },
        )
        assert response.status_code == 200
        messages = response.json()
        by_id = {message["message_id"]: message for message in messages}
        if all(
            by_id.get(message_id, {}).get("delivery_status") == "completed"
            for message_id in message_ids
        ):
            return messages
        await asyncio.sleep(0.01)
    raise AssertionError(f"messages for delegation {delegation_id} did not complete")


async def _wait_child_reports(
    client: httpx.AsyncClient,
    parent_delegation_id: str,
    child_delegation_ids: set[str],
) -> list[dict[str, Any]]:
    for _ in range(200):
        response = await client.get(
            f"/delegations/{parent_delegation_id}/events",
            params={
                "actor_id": _OBSERVER["principal_id"],
                "actor_kind": "human",
            },
        )
        assert response.status_code == 200
        messages = response.json()
        reported_children = {
            message["payload"].get("delegation_id")
            for message in messages
            if message["message_type"] == "result"
        }
        if child_delegation_ids <= reported_children:
            return messages
        await asyncio.sleep(0.01)
    raise AssertionError(f"child reports for delegation {parent_delegation_id} were not published")


async def _register_providers(
    runtime: InvocationRuntime,
    providers: dict[str, FakeAgentProvider],
) -> None:
    for provider_id, provider in providers.items():
        await runtime.register_provider(provider_id, provider)


@pytest.mark.asyncio
async def test_two_fake_children_complete_decision_message_and_restart_vertical(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "fake-delegation-vertical.jsonl"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    providers = {
        "fake-parent": FakeAgentProvider(
            FakeAgentScenario(output={"answer": "parent"}, delay_seconds=3.0)
        ),
        "fake-child-a": FakeAgentProvider(
            FakeAgentScenario(
                output={"answer": "child-a"},
                events=({"progress": "child-a-working"},),
                delay_seconds=0.01,
            )
        ),
        "fake-child-b": FakeAgentProvider(
            FakeAgentScenario(
                output={"answer": "child-b"},
                events=({"progress": "child-b-working"},),
                delay_seconds=0.01,
            )
        ),
    }
    runtime = InvocationRuntime()
    await _register_providers(runtime, providers)
    service = ControlPlaneService(
        runtime,
        state_path=state_path,
        cwd_policy=WorkingDirectoryPolicy((tmp_path,)),
    )
    await service.start()
    app = create_app(service)

    parent_payload = _delegation_payload(
        "vertical-parent",
        "fake-parent",
        "a" * 64,
        cwd=workspace,
        scope={"scope_id": "vertical-root"},
        child_scope={
            "scope_id": "vertical-parent-scope",
            "parent_scope_id": "vertical-root",
        },
    )
    child_payloads = [
        _delegation_payload(
            "vertical-child-a",
            "fake-child-a",
            "b" * 64,
            cwd=workspace,
            scope={
                "scope_id": "vertical-parent-scope",
                "parent_scope_id": "vertical-root",
            },
            child_scope={
                "scope_id": "vertical-child-a-scope",
                "parent_scope_id": "vertical-parent-scope",
            },
            parent_delegation_id="vertical-parent",
        ),
        _delegation_payload(
            "vertical-child-b",
            "fake-child-b",
            "c" * 64,
            cwd=workspace,
            scope={
                "scope_id": "vertical-parent-scope",
                "parent_scope_id": "vertical-root",
            },
            child_scope={
                "scope_id": "vertical-child-b-scope",
                "parent_scope_id": "vertical-parent-scope",
            },
            parent_delegation_id="vertical-parent",
        ),
    ]

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            parent = await _approve_and_create(client, parent_payload, providers["fake-parent"])
            assert parent["child_scope"]["scope_id"] == "vertical-parent-scope"

            child_a = await _approve_and_create(
                client, child_payloads[0], providers["fake-child-a"]
            )
            child_b = await _approve_and_create(
                client, child_payloads[1], providers["fake-child-b"]
            )
            assert child_a["parent_delegation_id"] == "vertical-parent"
            assert child_b["parent_delegation_id"] == "vertical-parent"

            children = await client.get(
                "/delegations/vertical-parent/children",
                params={
                    "actor_id": _OBSERVER["principal_id"],
                    "actor_kind": "human",
                },
            )
            assert children.status_code == 200
            assert [item["delegation_id"] for item in children.json()] == [
                "vertical-child-a",
                "vertical-child-b",
            ]

            first_child_a = await _wait_terminal(client, "vertical-child-a")
            child_b_terminal = await _wait_terminal(client, "vertical-child-b")
            assert first_child_a["report"]["output"] == {"answer": "child-a"}
            assert child_b_terminal["report"]["output"] == {"answer": "child-b"}

            question = await client.post(
                "/delegations/vertical-child-a/messages",
                json={
                    "actor": {
                        "principal_id": "delegation:vertical-child-a",
                        "kind": "agent",
                    },
                    "message_id": "vertical-child-a-question",
                    "message_type": "question",
                    "payload": {"question": "continue with the approved plan?"},
                    "recipient": _CONTROLLER,
                    "correlation_id": "vertical-child-a-correlation",
                },
            )
            assert question.status_code == 202
            assert question.json()["delivery_status"] == "accepted"

            reply = await client.post(
                "/delegations/vertical-child-a/reply",
                json={
                    "request_id": "vertical-child-a-reply",
                    "idempotency_key": "vertical-child-a-reply-idempotency",
                    "actor": _CONTROLLER,
                    "session_id": first_child_a["session_id"],
                    "message_id": "vertical-child-a-answer",
                    "expected_activation_id": first_child_a["report"]["source_activation_id"],
                    "input": {"answer": "continue"},
                    "correlation_id": "vertical-child-a-correlation",
                    "reply_to": "vertical-child-a-question",
                },
            )
            assert reply.status_code == 202
            child_a_terminal = await _wait_terminal(client, "vertical-child-a", activation_count=2)
            assert child_a_terminal["activation_count"] == 2
            assert providers["fake-child-a"].starts == 2

            child_a_messages = await _wait_messages_completed(
                client,
                "vertical-child-a",
                {"vertical-child-a-question", "vertical-child-a-answer"},
            )
            assert [message["sequence"] for message in child_a_messages] == list(
                range(1, len(child_a_messages) + 1)
            )
            by_id = {message["message_id"]: message for message in child_a_messages}
            assert by_id["vertical-child-a-question"]["delivery_status"] == "completed"
            assert by_id["vertical-child-a-answer"]["delivery_status"] == "completed"
            assert any(
                message["message_type"] == "progress"
                and message["payload"]["payload"] == {"progress": "child-a-working"}
                for message in child_a_messages
            )

            parent_terminal = await _wait_terminal(client, "vertical-parent")
            assert parent_terminal["report"]["output"] == {"answer": "parent"}
            parent_messages = await _wait_child_reports(
                client,
                "vertical-parent",
                {"vertical-child-a", "vertical-child-b"},
            )
            reported_children = {
                message["payload"].get("delegation_id")
                for message in parent_messages
                if message["message_type"] == "result"
            }
            assert {"vertical-child-a", "vertical-child-b"} <= reported_children
            question_sequence = by_id["vertical-child-a-question"]["sequence"]
    finally:
        await service.stop()
        await runtime.stop()

    restored_providers = {provider_id: FakeAgentProvider() for provider_id in providers}
    restored_runtime = InvocationRuntime()
    await _register_providers(restored_runtime, restored_providers)
    restored_service = ControlPlaneService(
        restored_runtime,
        state_path=state_path,
        cwd_policy=WorkingDirectoryPolicy((tmp_path,)),
    )
    await restored_service.start()
    restored_app = create_app(restored_service)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restored_app),
            base_url="http://test",
        ) as client:
            restored_parent = await client.get(
                "/delegations/vertical-parent",
                params={
                    "actor_id": _CONTROLLER["principal_id"],
                    "actor_kind": "application",
                },
            )
            assert restored_parent.status_code == 200
            assert restored_parent.json()["status"] == "completed"

            restored_children = await client.get(
                "/delegations/vertical-parent/children",
                params={
                    "actor_id": _OBSERVER["principal_id"],
                    "actor_kind": "human",
                },
            )
            assert restored_children.status_code == 200
            assert [item["delegation_id"] for item in restored_children.json()] == [
                "vertical-child-a",
                "vertical-child-b",
            ]
            assert all(item["status"] == "completed" for item in restored_children.json())

            replay = await client.get(
                "/delegations/vertical-child-a/events",
                params={
                    "actor_id": _OBSERVER["principal_id"],
                    "actor_kind": "human",
                    "next_sequence": question_sequence,
                },
            )
            assert replay.status_code == 200
            assert replay.json()[0]["message_id"] == "vertical-child-a-question"
            replay_by_id = {message["message_id"]: message for message in replay.json()}
            assert replay_by_id["vertical-child-a-question"]["delivery_status"] == "completed"
            assert replay_by_id["vertical-child-a-answer"]["delivery_status"] == "completed"

            restored_parent_events = await client.get(
                "/delegations/vertical-parent/events",
                params={
                    "actor_id": _OBSERVER["principal_id"],
                    "actor_kind": "human",
                },
            )
            restored_reported_children = {
                message["payload"].get("delegation_id")
                for message in restored_parent_events.json()
                if message["message_type"] == "result"
            }
            assert {"vertical-child-a", "vertical-child-b"} <= restored_reported_children
            assert all(provider.starts == 0 for provider in restored_providers.values())
    finally:
        await restored_service.stop()
        await restored_runtime.stop()
