from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
from misaka_control_plane import (
    ControlPlaneService,
    DelegationTriggerSubmission,
    WorkingDirectoryPolicy,
    create_app,
)
from misaka_control_plane.delegation_trigger import (
    TRIGGER_EVENT_INPUT_KEY,
    delegation_submission_from_trigger,
)
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_invocation_runtime import InvocationRuntime
from pydantic import ValidationError
from starlette.testclient import TestClient


def _trigger_payload(workspace: Path) -> dict[str, Any]:
    principal = {"principal_id": "event-router", "kind": "application"}
    return {
        "trigger_id": "repository-review",
        "event": {
            "event_id": "push-20260824-1",
            "source": "git.example/repository",
            "event_type": "dev.repository.push.v1",
            "subject": "refs/heads/main",
            "occurred_at": "2026-08-24T10:00:00Z",
            "data": {
                "repository": "example/project",
                "ref": "refs/heads/main",
                "commit": "a" * 40,
            },
        },
        "delegation": {
            "actor": principal,
            "initiator": principal,
            "controller": principal,
            "scope": {"scope_id": "repository-review"},
            "capability_id": "agent.invocation",
            "operation": "invoke",
            "input": {"prompt": "Review the repository event and report risks."},
            "cwd": str(workspace),
            "provider_id": "fake",
            "model": "fake/model",
            "effort": "high",
            "policy_context": {
                "sandbox": "read_only",
                "network_policy": "deny",
            },
            "output_schema": None,
            "plan_hash": "a" * 64,
            "mode": "continuable",
            "decision_ref": None,
        },
    }


def test_trigger_maps_event_identity_to_deterministic_delegation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    submission = DelegationTriggerSubmission.model_validate(_trigger_payload(workspace))

    first = delegation_submission_from_trigger(submission)
    repeated = delegation_submission_from_trigger(submission)
    another_route_payload = _trigger_payload(workspace)
    another_route_payload["trigger_id"] = "release-notes"
    another_route = delegation_submission_from_trigger(
        DelegationTriggerSubmission.model_validate(another_route_payload)
    )

    assert first.delegation_id == repeated.delegation_id
    assert first.idempotency_key == repeated.idempotency_key
    assert first.delegation_id != another_route.delegation_id
    assert first.idempotency_key != another_route.idempotency_key
    trigger_event = cast(dict[str, Any], first.input[TRIGGER_EVENT_INPUT_KEY])
    assert trigger_event["trigger_id"] == "repository-review"
    assert trigger_event["event_id"] == "push-20260824-1"
    assert trigger_event["event_type"] == "dev.repository.push.v1"
    assert trigger_event["data"] == {
        "repository": "example/project",
        "ref": "refs/heads/main",
        "commit": "a" * 40,
    }


def test_trigger_rejects_reserved_input_and_unsafe_event_data(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reserved = _trigger_payload(workspace)
    reserved["delegation"]["input"][TRIGGER_EVENT_INPUT_KEY] = {"spoofed": True}
    with pytest.raises(ValueError, match="owned by the trigger adapter"):
        delegation_submission_from_trigger(DelegationTriggerSubmission.model_validate(reserved))

    unsafe = _trigger_payload(workspace)
    unsafe["event"]["data"] = {"access_token": "secret"}
    with pytest.raises(ValidationError, match="not allowed through the Gateway"):
        DelegationTriggerSubmission.model_validate(unsafe)

    blank_identity = _trigger_payload(workspace)
    blank_identity["event"]["source"] = " "
    with pytest.raises(ValidationError, match="must not be blank"):
        DelegationTriggerSubmission.model_validate(blank_identity)


def test_trigger_endpoint_is_idempotent_and_rejects_conflicting_replay(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = FakeAgentProvider(
        FakeAgentScenario(
            output={"answer": "event handled"},
            delay_seconds=0.01,
        )
    )

    async def setup(runtime: InvocationRuntime) -> None:
        await runtime.register_provider("fake", provider)

    service = ControlPlaneService(
        InvocationRuntime(),
        state_path=tmp_path / "triggered-delegations.jsonl",
        provider_setup=setup,
        cwd_policy=WorkingDirectoryPolicy((tmp_path,)),
    )
    payload = _trigger_payload(workspace)

    with TestClient(create_app(service)) as client:
        first = client.post("/delegations/trigger", json=payload)
        repeated = client.post("/delegations/trigger", json=payload)
        conflicting_payload = deepcopy(payload)
        conflicting_payload["event"]["data"]["commit"] = "b" * 40
        conflicting = client.post("/delegations/trigger", json=conflicting_payload)
        listed = client.get(
            "/delegations",
            params={"actor_id": "event-router", "actor_kind": "application"},
        )

    assert first.status_code == 202, first.json()
    assert repeated.status_code == 202, repeated.json()
    assert first.json()["delegation_id"] == repeated.json()["delegation_id"]
    assert first.json()["session_id"] is not None
    assert first.json()["session_id"] == repeated.json()["session_id"]
    assert conflicting.status_code == 409
    assert "different facts" in conflicting.json()["detail"]
    assert listed.status_code == 200
    assert [item["delegation_id"] for item in listed.json()] == [first.json()["delegation_id"]]
    assert provider.starts == 1
