from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx2
from fastapi import FastAPI

from multi_agent_v2.apps.control_api.main import create_app
from multi_agent_v2.apps.control_api.routes import ControlApiDependencies, LivenessResponse
from multi_agent_v2.packages.config import Settings
from multi_agent_v2.packages.control_plane.commands import WorkflowCommandService
from multi_agent_v2.packages.control_plane.models import TemplateCreate, TemplateRecord
from multi_agent_v2.packages.control_plane.service import ControlPlaneService
from multi_agent_v2.packages.domain.events import CloudEventEnvelope, EventIngestResult
from multi_agent_v2.packages.eventing import WebhookPolicy
from multi_agent_v2.packages.observability.health import HealthReport, HealthService
from multi_agent_v2.packages.persistence import (
    ControlPlaneRepository,
    IdempotencyConflict,
)


class PassingProbe:
    name = "passing"

    async def check(self) -> None:
        return None


class FailingProbe:
    name = "failing"

    async def check(self) -> None:
        raise RuntimeError("private detail")


def _app(tmp_path: Path, *, healthy: bool) -> FastAPI:
    probe = PassingProbe() if healthy else FailingProbe()
    service = HealthService([probe], timeout_seconds=0.1)
    settings = Settings(artifact_root=tmp_path)
    app = create_app(settings, health_service=service)

    async def _test_write() -> dict[str, str]:
        return {"status": "ok"}

    app.add_api_route("/_test/write", _test_write, methods=["POST"])
    return app


class _ControlService:
    def __init__(self, *, conflict: bool = False) -> None:
        self.conflict = conflict

    async def create_template(
        self,
        command: TemplateCreate,
        *,
        idempotency_key: str,
    ) -> TemplateRecord:
        if self.conflict:
            raise IdempotencyConflict("reused")
        now = datetime.now(UTC)
        return TemplateRecord(
            template_id=command.template_id,
            name=command.name,
            description=command.description,
            latest_version=0,
            revision=1,
            created_at=now,
            updated_at=now,
        )

    def list_workspace_ids(self) -> tuple[str, ...]:
        return ("repo", "docs")


class _ControlRepository:
    def __init__(self) -> None:
        self.events: list[CloudEventEnvelope] = []

    async def ingest_event(self, event: CloudEventEnvelope) -> EventIngestResult:
        self.events.append(event)
        return EventIngestResult(inbox_id=event.id, duplicate=False)


def _control_dependencies(
    *,
    service: _ControlService | None = None,
    repository: _ControlRepository | None = None,
    webhook_policy: WebhookPolicy | None = None,
    maximum_event_bytes: int = 1024,
) -> ControlApiDependencies:
    return ControlApiDependencies(
        service=cast(ControlPlaneService, service or _ControlService()),
        repository=cast(ControlPlaneRepository, repository or _ControlRepository()),
        commands=cast(WorkflowCommandService, object()),
        webhook_policy=webhook_policy,
        maximum_event_bytes=maximum_event_bytes,
    )


@asynccontextmanager
async def _client(tmp_path: Path, *, healthy: bool) -> AsyncGenerator[httpx2.AsyncClient]:
    app = _app(tmp_path, healthy=healthy)
    transport = httpx2.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


@asynccontextmanager
async def _control_client(
    tmp_path: Path,
    dependencies: ControlApiDependencies,
) -> AsyncGenerator[httpx2.AsyncClient]:
    service = HealthService([PassingProbe()], timeout_seconds=0.1)
    app = create_app(
        Settings(artifact_root=tmp_path),
        health_service=service,
        control_dependencies=dependencies,
    )
    transport = httpx2.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


async def test_live_does_not_depend_on_components(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=False) as client:
        response = await client.get("/live")

    assert response.status_code == 200
    assert LivenessResponse.model_validate_json(response.content).status == "ok"


async def test_ready_returns_200_when_components_are_up(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=True) as client:
        response = await client.get("/ready")

    report = HealthReport.model_validate_json(response.content)
    assert response.status_code == 200
    assert report.status == "ready"


async def test_ready_returns_503_without_leaking_failure_message(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=False) as client:
        response = await client.get("/ready")

    report = HealthReport.model_validate_json(response.content)
    assert response.status_code == 503
    assert report.status == "not_ready"
    assert report.components[0].detail == "RuntimeError"
    assert "private detail" not in response.text


async def test_component_health_remains_queryable_while_not_ready(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=False) as client:
        response = await client.get("/health/components")

    report = HealthReport.model_validate_json(response.content)
    assert response.status_code == 200
    assert report.status == "not_ready"


async def test_control_api_rejects_untrusted_host_header(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=True) as client:
        response = await client.get("/live", headers={"host": "untrusted.example"})

    assert response.status_code == 400


async def test_control_api_allows_configured_origin_for_write(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=True) as client:
        response = await client.post(
            "/_test/write",
            headers={"origin": "http://testserver"},
        )

    assert response.status_code == 200


async def test_control_api_allows_loopback_server_call_without_origin(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=True) as client:
        response = await client.post("/_test/write")

    assert response.status_code == 200


async def test_control_api_rejects_cross_origin_write(tmp_path: Path) -> None:
    async with _client(tmp_path, healthy=True) as client:
        response = await client.post(
            "/_test/write",
            headers={"origin": "https://evil.example"},
        )

    assert response.status_code == 403


async def test_template_create_requires_and_echoes_idempotent_command(tmp_path: Path) -> None:
    async with _control_client(tmp_path, _control_dependencies()) as client:
        missing = await client.post(
            "/api/v2/templates",
            json={"templateId": "addition", "name": "Addition"},
        )
        created = await client.post(
            "/api/v2/templates",
            headers={"Idempotency-Key": "template-addition"},
            json={"templateId": "addition", "name": "Addition"},
        )

    assert missing.status_code == 422
    assert created.status_code == 201
    assert created.json()["templateId"] == "addition"


async def test_idempotency_conflict_is_mapped_without_internal_detail(tmp_path: Path) -> None:
    async with _control_client(
        tmp_path,
        _control_dependencies(service=_ControlService(conflict=True)),
    ) as client:
        response = await client.post(
            "/api/v2/templates",
            headers={"Idempotency-Key": "reused"},
            json={"templateId": "addition", "name": "Addition"},
        )

    assert response.status_code == 409
    assert response.json() == {"error": "idempotency_conflict"}
    assert "reused" not in response.text


async def test_workspace_catalog_is_served_from_the_server_allowlist(tmp_path: Path) -> None:
    async with _control_client(tmp_path, _control_dependencies()) as client:
        response = await client.get("/api/v2/catalog/workspaces")

    assert response.status_code == 200
    assert response.json() == ["repo", "docs"]


async def test_event_ingress_rejects_oversized_body_before_parsing(tmp_path: Path) -> None:
    repository = _ControlRepository()
    async with _control_client(
        tmp_path,
        _control_dependencies(repository=repository, maximum_event_bytes=16),
    ) as client:
        response = await client.post(
            "/api/v2/events",
            content=b"x" * 17,
            headers={"content-type": "application/cloudevents+json"},
        )

    assert response.status_code == 413
    assert repository.events == []


async def test_webhook_ingress_binds_hmac_to_source_and_nonce(tmp_path: Path) -> None:
    secret = b"webhook-secret"
    source = "build"
    nonce = "delivery-1"
    timestamp = str(int(datetime.now(UTC).timestamp()))
    body = b'{"status":"ok"}'
    signature = hmac.new(
        secret,
        (timestamp.encode() + b"\n" + source.encode() + b"\n" + nonce.encode() + b"\n" + body),
        hashlib.sha256,
    ).hexdigest()
    repository = _ControlRepository()
    dependencies = _control_dependencies(
        repository=repository,
        webhook_policy=WebhookPolicy(secret=secret),
    )
    headers = {
        "X-Misaka-Timestamp": timestamp,
        "X-Misaka-Nonce": nonce,
        "X-Misaka-Signature": f"sha256={signature}",
        "content-type": "application/json",
    }

    async with _control_client(tmp_path, dependencies) as client:
        accepted = await client.post(
            f"/api/v2/webhooks/{source}",
            headers=headers,
            content=body,
        )
        replayed_to_other_source = await client.post(
            "/api/v2/webhooks/other",
            headers=headers,
            content=body,
        )

    assert accepted.status_code == 202
    assert accepted.json()["inboxId"] == nonce
    assert replayed_to_other_source.status_code == 422
    assert [event.id for event in repository.events] == [nonce]
