from __future__ import annotations

from pathlib import Path

from aitools_service_manager.app import create_app
from aitools_service_manager.config import ManagementConfig
from aitools_service_manager.service import ManagementService
from starlette.testclient import TestClient
from test_management_service import FakeControlPlaneClient, FakeLocalServices


def test_management_api_exposes_bootstrap_catalog_and_group_actions(tmp_path: Path) -> None:
    local = FakeLocalServices()
    control_plane = FakeControlPlaneClient()
    service = ManagementService(ManagementConfig(root=tmp_path), local, control_plane)

    with TestClient(create_app(service)) as client:
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"

        configuration = client.get("/configuration")
        assert configuration.status_code == 200
        assert configuration.json()["profile"] == "fake"
        assert configuration.json()["allowed_path_roots"] == []

        catalog = client.get("/services")
        assert catalog.status_code == 200
        assert catalog.json()[0]["service_id"] == "control-plane"

        started = client.post("/groups/core/start")
        assert started.status_code == 200
        assert started.json()["group_id"] == "core"
        assert local.statuses["control-plane"].value == "running"
        assert local.statuses["web-v3"].value == "running"


def test_management_api_returns_conflict_for_stale_epoch(tmp_path: Path) -> None:
    local = FakeLocalServices()
    local.set_running("control-plane", epoch=2)
    service = ManagementService(
        ManagementConfig(root=tmp_path),
        local,
        FakeControlPlaneClient(),
    )

    with TestClient(create_app(service)) as client:
        response = client.post("/services/control-plane/stop?epoch=1")

    assert response.status_code == 409
    assert "stale" in response.json()["detail"]


def test_management_api_updates_runtime_configuration_only_while_stopped(
    tmp_path: Path,
) -> None:
    local = FakeLocalServices()
    service = ManagementService(
        ManagementConfig(root=tmp_path),
        local,
        FakeControlPlaneClient(),
    )
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    payload = {
        "profile": "fake",
        "codex_home": None,
        "provider_id": "fake",
        "network_deny_enforced": False,
        "allowed_path_roots": [str(allowed)],
    }

    with TestClient(create_app(service)) as client:
        updated = client.put("/configuration", json=payload)
        assert updated.status_code == 200
        assert updated.json()["allowed_path_roots"] == [str(allowed.resolve())]

        assert client.post("/groups/core/start").status_code == 200
        rejected = client.put("/configuration", json={**payload, "allowed_path_roots": []})

    assert rejected.status_code == 409
    assert "stop the core services" in rejected.json()["detail"]
