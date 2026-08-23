from __future__ import annotations

from pathlib import Path
from typing import Any

from aitools_service_manager.app import create_app
from aitools_service_manager.config import ManagementConfig
from aitools_service_manager.service import ManagementService
from starlette.testclient import TestClient
from test_management_service import FakeControlPlaneClient, FakeDirectoryPicker, FakeLocalServices


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
        assert configuration.json()["providers"] == [
            {
                "provider_id": "fake",
                "kind": "fake",
                "codex_home": None,
                "config_overrides": [],
                "claude_config_dir": None,
                "claude_cli_path": None,
                "model_ids": [],
                "network_deny_enforced": False,
            }
        ]
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
    payload: dict[str, Any] = {
        "providers": [
            {
                "provider_id": "fake",
                "kind": "fake",
                "codex_home": None,
                "config_overrides": [],
                "network_deny_enforced": False,
            }
        ],
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


def test_management_api_opens_directory_picker_and_returns_selected_path(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    picker = FakeDirectoryPicker(selected)
    service = ManagementService(
        ManagementConfig(root=tmp_path),
        FakeLocalServices(),
        FakeControlPlaneClient(),
        directory_picker=picker,
    )

    with TestClient(create_app(service)) as client:
        response = client.post(
            "/configuration/select-directory",
            json={"initial_path": str(tmp_path)},
        )

    assert response.status_code == 200
    assert response.json() == {"path": str(selected.resolve())}
    assert picker.initial_path == tmp_path


def test_management_api_returns_null_when_directory_picker_is_cancelled(tmp_path: Path) -> None:
    picker = FakeDirectoryPicker()
    service = ManagementService(
        ManagementConfig(root=tmp_path),
        FakeLocalServices(),
        FakeControlPlaneClient(),
        directory_picker=picker,
    )

    with TestClient(create_app(service)) as client:
        response = client.post("/configuration/select-directory", json={"initial_path": None})

    assert response.status_code == 200
    assert response.json() == {"path": None}
