from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from misaka_approval_capability import MemoryDecisionStore
from misaka_control_plane import (
    ControlPlaneService,
    DelegationApprovalSubmission,
    DelegationReplySubmission,
    DelegationSubmission,
    WorkspaceCatalog,
    create_app,
)
from misaka_control_plane.delegation_gateway_policy import (
    DelegationDecisionGate,
    delegation_continuation_input,
    delegation_request_from_submission,
)
from misaka_delegation_capability import DelegationCapabilityRejected
from misaka_delegation_contracts import (
    DelegationPolicy,
    DelegationRef,
    DelegationRequest,
    DelegationSnapshot,
    DelegationStatus,
)
from misaka_delegation_runtime import DelegationRuntime
from misaka_fake_agent import FakeAgentProvider, FakeAgentScenario
from misaka_interaction_contracts import DecisionRef, PrincipalKind, PrincipalRef, ScopeRef
from misaka_interaction_memory import MemoryInteractionChannelStore
from misaka_invocation_runtime import InvocationRuntime
from pydantic import ValidationError
from starlette.testclient import TestClient

_PLAN_HASH = "a" * 64


def _submission_payload(*, workspace_id: str = "workspace") -> dict[str, Any]:
    return {
        "actor": {"principal_id": "client", "kind": "application"},
        "delegation_id": "delegation-1",
        "idempotency_key": "delegation-1-idem",
        "initiator": {"principal_id": "client", "kind": "application"},
        "controller": {"principal_id": "client", "kind": "application"},
        "scope": {"scope_id": "scope-1"},
        "capability_id": "agent.invocation",
        "operation": "invoke",
        "input": {"prompt": "inspect the workspace"},
        "workspace_id": workspace_id,
        "provider_id": "fake",
        "model": "fake/model",
        "effort": "high",
        "policy_context": {},
        "output_schema": None,
        "plan_hash": _PLAN_HASH,
        "decision_ref": None,
    }


@pytest.mark.parametrize(
    "field_name",
    [
        "workspace_id",
        "provider_id",
        "model",
        "effort",
        "policy_context",
        "output_schema",
        "plan_hash",
        "decision_ref",
    ],
)
def test_delegation_submission_requires_explicit_gateway_contract_fields(
    field_name: str,
) -> None:
    payload = _submission_payload()
    del payload[field_name]

    with pytest.raises(ValidationError):
        DelegationSubmission.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "unsafe_value"),
    [
        ("input", {"cwd": "D:/outside"}),
        ("input", {"nested": {"command": "dangerous"}}),
        ("input", {"nested": {"command-line": "dangerous"}}),
        ("input", {"nested": {"env": {"TOKEN": "secret"}}}),
        ("input", {"environmentVariables": {"VALUE": "secret"}}),
        ("input", {"access_token": "secret"}),
        ("input", {"apiKey": "secret"}),
        ("input", {"sandbox": "danger-full-access"}),
        ("input", {"provider_client": object()}),
        ("input", {"providerSdk": object()}),
        ("input", {"_misaka_gateway": {"trusted": False}}),
        ("input", {"value": object()}),
        ("policy_context", {"cwd": "D:/outside"}),
        ("policy_context", {"command": "dangerous"}),
        ("policy_context", {"environment": {"API_KEY": "secret"}}),
        ("policy_context", {"credential": "secret"}),
    ],
)
def test_delegation_submission_rejects_unsafe_gateway_values(
    section: str,
    unsafe_value: dict[str, object],
) -> None:
    payload = _submission_payload()
    payload[section] = unsafe_value

    with pytest.raises(ValidationError):
        DelegationSubmission.model_validate(payload)


@pytest.mark.parametrize(
    "policy_context",
    [
        {"sandbox": "danger-full-access"},
        {"network_policy": "inherit"},
        {"sandbox": "read_only", "unknown_policy": True},
    ],
)
def test_delegation_submission_rejects_unapproved_policy_context(
    policy_context: dict[str, object],
) -> None:
    payload = _submission_payload()
    payload["policy_context"] = policy_context

    with pytest.raises(ValidationError):
        DelegationSubmission.model_validate(payload)


@pytest.mark.parametrize(
    "unsafe_input",
    [
        {"cwd": "D:/outside"},
        {"sandbox": "workspace_write"},
        {"nested": {"command": "dangerous"}},
        {"access_token": "secret"},
    ],
)
def test_delegation_reply_rejects_gateway_owned_or_unsafe_input(
    unsafe_input: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        DelegationReplySubmission.model_validate(
            {
                "request_id": "reply-1",
                "idempotency_key": "reply-1-idem",
                "actor": {"principal_id": "client", "kind": "application"},
                "session_id": "session-1",
                "message_id": "answer-1",
                "expected_activation_id": "activation-1",
                "input": unsafe_input,
                "correlation_id": "correlation-1",
                "reply_to": "question-1",
            }
        )


def test_workspace_catalog_builds_internal_request_without_client_path_control(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    submission = DelegationSubmission.model_validate(_submission_payload())

    request = delegation_request_from_submission(
        submission,
        WorkspaceCatalog({"workspace": workspace}),
    )

    assert request.input == {
        "prompt": "inspect the workspace",
        "cwd": str(workspace.resolve()),
        "sandbox": "read_only",
    }
    assert request.constraints == {
        "network_policy": "deny",
        "_misaka_gateway": {
            "workspace_id": "workspace",
            "plan_hash": _PLAN_HASH,
            "policy_context": {
                "sandbox": "read_only",
                "network_policy": "deny",
            },
        },
    }


def test_continuation_reuses_trusted_gateway_context(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = delegation_request_from_submission(
        DelegationSubmission.model_validate(_submission_payload()),
        WorkspaceCatalog({"workspace": workspace}),
    )
    snapshot = DelegationSnapshot(
        ref=DelegationRef(request.delegation_id),
        request=request,
        status=DelegationStatus.COMPLETED,
    )

    assert delegation_continuation_input(snapshot, {"prompt": "continue"}) == {
        "prompt": "continue",
        "cwd": str(workspace.resolve()),
        "sandbox": "read_only",
    }
    with pytest.raises(DelegationCapabilityRejected, match="cannot override"):
        delegation_continuation_input(snapshot, {"cwd": "D:/outside"})


@pytest.mark.parametrize(
    "catalog",
    [
        {},
        {"workspace": "missing-directory"},
    ],
)
def test_workspace_catalog_fails_closed_for_unavailable_workspace(
    tmp_path: Path,
    catalog: dict[str, str],
) -> None:
    entries = {key: tmp_path / value for key, value in catalog.items()}
    submission = DelegationSubmission.model_validate(_submission_payload())

    with pytest.raises(ValueError, match="workspace"):
        delegation_request_from_submission(submission, WorkspaceCatalog(entries))


def test_workspace_catalog_translates_path_resolution_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = WorkspaceCatalog({"workspace": Path("workspace")})

    def fail_resolution(_path: Path, *, strict: bool = False) -> Path:
        del strict
        raise RuntimeError("symlink loop")

    monkeypatch.setattr(Path, "resolve", fail_resolution)

    with pytest.raises(ValueError, match="workspace workspace is unavailable"):
        catalog.resolve("workspace")


@pytest.mark.asyncio
async def test_delegation_decision_gate_blocks_runtime_bypass_before_provider_start() -> None:
    invocation_runtime = InvocationRuntime()
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "blocked"}))
    await invocation_runtime.register_provider("fake", provider)
    decision_store = MemoryDecisionStore()
    runtime = DelegationRuntime(
        invocation_runtime,
        MemoryInteractionChannelStore(),
        gate=DelegationDecisionGate(decision_store),
    )
    actor = PrincipalRef("client", PrincipalKind.APPLICATION)
    request = DelegationRequest(
        delegation_id="runtime-bypass",
        idempotency_key="runtime-bypass-idem",
        initiator=actor,
        controller=actor,
        scope=ScopeRef("scope-1"),
        capability_id="agent.invocation",
        operation="invoke",
        input={
            "prompt": "must wait for approval",
            "cwd": str(Path.cwd()),
            "sandbox": "read_only",
        },
        provider_id="fake",
        model="fake/model",
        effort="high",
        decision_ref=DecisionRef("runtime-bypass-decision", 1),
        constraints={
            "network_policy": "deny",
            "_misaka_gateway": {
                "workspace_id": "workspace",
                "plan_hash": _PLAN_HASH,
                "policy_context": {
                    "sandbox": "read_only",
                    "network_policy": "deny",
                },
            },
        },
        policy=DelegationPolicy(require_decision=True),
    )
    try:
        snapshot = await (await runtime.submit(request)).snapshot()
        assert snapshot.status is DelegationStatus.REJECTED
        assert snapshot.admission is not None
        assert snapshot.admission.error_code == "decision.required"
        assert provider.starts == 0
        assert len(await decision_store.list()) == 1
    finally:
        await runtime.stop()
        await invocation_runtime.stop()


def test_delegation_approval_is_bound_and_requires_create_retry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = InvocationRuntime()
    provider = FakeAgentProvider(FakeAgentScenario(output={"answer": "approved"}))

    async def setup(current_runtime: InvocationRuntime) -> None:
        await current_runtime.register_provider("fake", provider)

    service = ControlPlaneService(
        runtime,
        state_path=tmp_path / "approval.jsonl",
        provider_setup=setup,
        workspace_catalog=WorkspaceCatalog({"workspace": workspace}),
    )
    app = create_app(service)
    payload = _submission_payload()
    payload["policy"] = {
        "require_decision": True,
        "requested_effects": ["workspace.read"],
    }
    payload["decision_ref"] = {
        "proposal_id": "delegation-1-decision",
        "revision": 1,
    }

    with TestClient(app) as client:
        pending = client.post("/delegations", json=payload)
        assert pending.status_code == 409
        assert "pending" in pending.json()["detail"]
        assert provider.starts == 0

        missing = client.get(
            "/delegations/delegation-1",
            params={"actor_id": "client", "actor_kind": "application"},
        )
        assert missing.status_code == 404

        generic = client.post(
            "/decisions/delegation-1-decision/revisions/1/decision",
            json={
                "decision": "approved",
                "principal_id": "reviewer",
                "reason": "wrong endpoint",
            },
        )
        assert generic.status_code == 409
        assert (
            client.get("/decisions/delegation-1-decision/revisions/1").json()["status"] == "pending"
        )

        approval = {
            "actor": {"principal_id": "reviewer", "kind": "human"},
            "decision_ref": payload["decision_ref"],
            "plan_hash": _PLAN_HASH,
            "reason": "reviewed",
        }
        approved = client.post("/delegations/delegation-1/approve", json=approval)
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approved.json()["delegation_id"] == "delegation-1"
        assert provider.starts == 0

        retried = client.post("/delegations", json=payload)
        assert retried.status_code == 202
        assert retried.json()["delegation_id"] == "delegation-1"

        observed = None
        for _ in range(100):
            observed = client.get(
                "/delegations/delegation-1",
                params={"actor_id": "client", "actor_kind": "application"},
            )
            if observed.json()["status"] == "completed":
                break
        assert observed is not None
        assert observed.json()["status"] == "completed"
        assert provider.starts == 1

        idempotent = client.post("/delegations/delegation-1/approve", json=approval)
        assert idempotent.status_code == 200

        changed_reason = {**approval, "reason": "different review"}
        assert (
            client.post("/delegations/delegation-1/approve", json=changed_reason).status_code == 409
        )
        changed_actor = deepcopy(approval)
        changed_actor["actor"] = {
            "principal_id": "other-reviewer",
            "kind": "human",
        }
        assert (
            client.post("/delegations/delegation-1/approve", json=changed_actor).status_code == 409
        )


@pytest.mark.parametrize(
    "mutation",
    [
        ("input", {"prompt": "changed"}),
        ("workspace_id", "workspace-2"),
        ("provider_id", "fake-2"),
        ("model", "fake/other"),
        ("effort", "low"),
        ("policy_context", {"sandbox": "workspace_write"}),
        ("plan_hash", "b" * 64),
        ("scope", {"scope_id": "scope-2"}),
        ("policy", {"require_decision": True, "requested_effects": ["other"]}),
    ],
)
def test_delegation_decision_rejects_reused_ref_with_changed_binding(
    tmp_path: Path,
    mutation: tuple[str, object],
) -> None:
    workspace = tmp_path / "workspace"
    workspace_2 = tmp_path / "workspace-2"
    workspace.mkdir()
    workspace_2.mkdir()
    runtime = InvocationRuntime()
    service = ControlPlaneService(
        runtime,
        state_path=tmp_path / "binding.jsonl",
        workspace_catalog=WorkspaceCatalog({"workspace": workspace, "workspace-2": workspace_2}),
    )
    app = create_app(service)
    payload = _submission_payload()
    payload["policy"] = {"require_decision": True}
    payload["decision_ref"] = {"proposal_id": "binding-decision", "revision": 1}
    changed = deepcopy(payload)
    changed[mutation[0]] = mutation[1]

    with TestClient(app) as client:
        assert client.post("/delegations", json=payload).status_code == 409
        conflict = client.post("/delegations", json=changed)
        assert conflict.status_code == 409
        assert "different plan" in conflict.json()["detail"]


def test_delegation_approve_rejects_wrong_actor_path_and_plan_hash(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = InvocationRuntime()
    service = ControlPlaneService(
        runtime,
        state_path=tmp_path / "approval-binding.jsonl",
        workspace_catalog=WorkspaceCatalog({"workspace": workspace}),
    )
    app = create_app(service)
    payload = _submission_payload()
    payload["policy"] = {"require_decision": True}
    payload["decision_ref"] = {"proposal_id": "approval-binding", "revision": 1}

    with TestClient(app) as client:
        assert client.post("/delegations", json=payload).status_code == 409
        base = {
            "actor": {"principal_id": "reviewer", "kind": "human"},
            "decision_ref": payload["decision_ref"],
            "plan_hash": _PLAN_HASH,
            "reason": "reviewed",
        }
        non_human = deepcopy(base)
        non_human["actor"] = {
            "principal_id": "reviewer",
            "kind": "application",
        }
        assert client.post("/delegations/delegation-1/approve", json=non_human).status_code == 403
        assert client.post("/delegations/other/approve", json=base).status_code == 409
        stale = {**base, "plan_hash": "b" * 64}
        assert client.post("/delegations/delegation-1/approve", json=stale).status_code == 409
        wrong_revision = deepcopy(base)
        wrong_revision["decision_ref"] = {
            "proposal_id": "approval-binding",
            "revision": 2,
        }
        assert (
            client.post("/delegations/delegation-1/approve", json=wrong_revision).status_code == 404
        )
        assert client.get("/decisions/approval-binding/revisions/1").json()["status"] == "pending"


def test_delegation_approval_submission_requires_human_bound_fields() -> None:
    approval = DelegationApprovalSubmission.model_validate(
        {
            "actor": {"principal_id": "reviewer", "kind": "human"},
            "decision_ref": {"proposal_id": "decision", "revision": 1},
            "plan_hash": _PLAN_HASH,
            "reason": "reviewed",
        }
    )
    assert approval.actor.kind is PrincipalKind.HUMAN
