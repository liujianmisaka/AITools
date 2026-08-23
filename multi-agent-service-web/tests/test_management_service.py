from __future__ import annotations

from pathlib import Path

import pytest
from aitools_service_manager.client import ControlPlaneRequestError
from aitools_service_manager.config import ManagementConfig
from aitools_service_manager.directory_picker import DirectoryPickerError
from aitools_service_manager.models import (
    ControlPlaneServicePayload,
    ManagementConfigurationUpdate,
    ProviderConfigurationUpdate,
)
from aitools_service_manager.service import ManagementService, ManagementServiceError
from misaka_service_runtime import (
    ManagedServiceStatus,
    ServiceConflict,
    ServiceSnapshot,
)


class FakeLocalServices:
    def __init__(self) -> None:
        self.statuses = {
            "control-plane": ManagedServiceStatus.STOPPED,
            "web-v3": ManagedServiceStatus.STOPPED,
        }
        self.epochs = {"control-plane": 0, "web-v3": 0}
        self.events: list[str] = []
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.started = False

    async def list(self) -> tuple[ServiceSnapshot, ...]:
        return (self._snapshot("control-plane"), self._snapshot("web-v3"))

    async def get(self, service_id: str) -> ServiceSnapshot:
        return self._snapshot(service_id)

    async def start_service(
        self, service_id: str, *, expected_epoch: int | None = None
    ) -> ServiceSnapshot:
        self._check_epoch(service_id, expected_epoch)
        self.events.append(f"start:{service_id}")
        if self.statuses[service_id] is not ManagedServiceStatus.RUNNING:
            self.epochs[service_id] += 1
            self.statuses[service_id] = ManagedServiceStatus.RUNNING
        return self._snapshot(service_id)

    async def stop(self, service_id: str, *, expected_epoch: int | None = None) -> ServiceSnapshot:
        self._check_epoch(service_id, expected_epoch)
        self.events.append(f"stop:{service_id}")
        self.statuses[service_id] = ManagedServiceStatus.STOPPED
        return self._snapshot(service_id)

    def set_running(self, service_id: str, *, epoch: int = 1) -> None:
        self.statuses[service_id] = ManagedServiceStatus.RUNNING
        self.epochs[service_id] = epoch

    def _check_epoch(self, service_id: str, expected_epoch: int | None) -> None:
        if expected_epoch is not None and expected_epoch != self.epochs[service_id]:
            raise ServiceConflict("service.epoch_fenced", "stale epoch")

    def _snapshot(self, service_id: str) -> ServiceSnapshot:
        is_control_plane = service_id == "control-plane"
        return ServiceSnapshot(
            service_id=service_id,
            display_name="Control Plane" if is_control_plane else "Web V3",
            description="test service",
            category="Core" if is_control_plane else "Application",
            status=self.statuses[service_id],
            controllable=True,
            endpoint=("http://127.0.0.1:8016" if is_control_plane else "http://127.0.0.1:5173"),
            epoch=self.epochs[service_id],
        )


class FakeControlPlaneClient:
    def __init__(self) -> None:
        self.available = True
        self.services = {
            "a2a-node": _remote_service("a2a-node", "A2A Node"),
            "a2a-agent-host": _remote_service("a2a-agent-host", "A2A Agent Host"),
        }
        self.events: list[str] = []

    async def list_services(self) -> list[ControlPlaneServicePayload]:
        self._require_available()
        return list(self.services.values())

    async def get_service(self, service_id: str) -> ControlPlaneServicePayload:
        self._require_available()
        return self.services[service_id]

    async def start_service(
        self, service_id: str, *, expected_epoch: int
    ) -> ControlPlaneServicePayload:
        self._require_available()
        current = self.services[service_id]
        _check_remote_epoch(current, expected_epoch)
        self.events.append(f"start:{service_id}")
        if current.status != "running":
            current = current.model_copy(update={"status": "running", "epoch": current.epoch + 1})
            self.services[service_id] = current
        return current

    async def stop_service(
        self, service_id: str, *, expected_epoch: int
    ) -> ControlPlaneServicePayload:
        self._require_available()
        current = self.services[service_id]
        _check_remote_epoch(current, expected_epoch)
        self.events.append(f"stop:{service_id}")
        current = current.model_copy(update={"status": "stopped"})
        self.services[service_id] = current
        return current

    def set_running(self, service_id: str, *, epoch: int = 1) -> None:
        self.services[service_id] = self.services[service_id].model_copy(
            update={"status": "running", "epoch": epoch}
        )

    def _require_available(self) -> None:
        if not self.available:
            raise ControlPlaneRequestError("control plane unavailable")


class FakeDirectoryPicker:
    def __init__(self, selected: Path | None = None, error: Exception | None = None) -> None:
        self.selected = selected
        self.error = error
        self.initial_path: Path | None = None

    def choose(self, initial_path: Path | None = None) -> Path | None:
        self.initial_path = initial_path
        if self.error is not None:
            raise self.error
        return self.selected


def _remote_service(service_id: str, display_name: str) -> ControlPlaneServicePayload:
    return ControlPlaneServicePayload(
        service_id=service_id,
        display_name=display_name,
        description="delegated service",
        category="A2A",
        status="stopped",
        controllable=True,
        endpoint=f"http://127.0.0.1/{service_id}",
    )


def _check_remote_epoch(service: ControlPlaneServicePayload, expected_epoch: int) -> None:
    if service.epoch != expected_epoch:
        raise ControlPlaneRequestError("stale epoch", status_code=409)


def _service(tmp_path: Path) -> tuple[ManagementService, FakeLocalServices, FakeControlPlaneClient]:
    local = FakeLocalServices()
    control_plane = FakeControlPlaneClient()
    config = ManagementConfig(root=tmp_path)
    return ManagementService(config, local, control_plane), local, control_plane


@pytest.mark.asyncio
async def test_service_catalog_is_available_before_control_plane_starts(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    services = await service.services()
    by_id = {item.service_id: item for item in services}

    assert [item.service_id for item in services] == [
        "control-plane",
        "web-v3",
        "a2a-node",
        "a2a-agent-host",
        "multi-agent-mcp",
    ]
    assert by_id["control-plane"].status == "stopped"
    assert by_id["a2a-node"].status == "unavailable"
    assert by_id["a2a-node"].controllable
    assert by_id["multi-agent-mcp"].status == "on_demand"
    assert not by_id["multi-agent-mcp"].controllable


@pytest.mark.asyncio
async def test_configuration_update_persists_for_the_next_control_plane_start(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)
    codex_home = tmp_path / "codex-home"
    allowed = tmp_path / "allowed"
    codex_home.mkdir()
    allowed.mkdir()

    updated = await service.update_configuration(
        ManagementConfigurationUpdate(
            providers=[
                ProviderConfigurationUpdate(
                    provider_id="fake-local",
                    kind="fake",
                    codex_home=None,
                    config_overrides=[],
                    network_deny_enforced=False,
                ),
                ProviderConfigurationUpdate(
                    provider_id="codex-local",
                    kind="codex",
                    codex_home=str(codex_home),
                    config_overrides=['model_provider="local"'],
                    network_deny_enforced=True,
                ),
            ],
            allowed_path_roots=[str(allowed)],
        )
    )

    assert [provider.provider_id for provider in updated.providers] == [
        "fake-local",
        "codex-local",
    ]
    assert updated.providers[1].codex_home == str(codex_home.resolve())
    assert updated.providers[1].config_overrides == ['model_provider="local"']
    assert updated.allowed_path_roots == [str(allowed.resolve())]
    restored, _, _ = _service(tmp_path)
    assert restored.configuration() == updated


@pytest.mark.asyncio
async def test_configuration_update_is_rejected_while_control_plane_runs(
    tmp_path: Path,
) -> None:
    service, local, _ = _service(tmp_path)
    local.set_running("control-plane")

    with pytest.raises(ManagementServiceError, match="stop the core services"):
        await service.update_configuration(
            ManagementConfigurationUpdate(
                providers=[
                    ProviderConfigurationUpdate(
                        provider_id="fake",
                        kind="fake",
                        codex_home=None,
                        config_overrides=[],
                        network_deny_enforced=False,
                    )
                ],
                allowed_path_roots=[],
            )
        )

    assert service.configuration().providers[0].provider_id == "fake"


@pytest.mark.asyncio
async def test_configuration_update_rejects_unavailable_path_filter(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    with pytest.raises(ManagementServiceError, match="unavailable") as raised:
        await service.update_configuration(
            ManagementConfigurationUpdate(
                providers=[
                    ProviderConfigurationUpdate(
                        provider_id="fake",
                        kind="fake",
                        codex_home=None,
                        config_overrides=[],
                        network_deny_enforced=False,
                    )
                ],
                allowed_path_roots=[str(tmp_path / "missing")],
            )
        )

    assert raised.value.status_code == 422


@pytest.mark.asyncio
async def test_choose_directory_returns_host_selected_absolute_path(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    picker = FakeDirectoryPicker(selected)
    service = ManagementService(
        ManagementConfig(root=tmp_path),
        FakeLocalServices(),
        FakeControlPlaneClient(),
        directory_picker=picker,
    )

    chosen = await service.choose_directory(str(tmp_path))

    assert chosen == selected
    assert picker.initial_path == tmp_path


@pytest.mark.asyncio
async def test_choose_directory_translates_native_picker_failure(tmp_path: Path) -> None:
    picker = FakeDirectoryPicker(error=DirectoryPickerError("picker failed"))
    service = ManagementService(
        ManagementConfig(root=tmp_path),
        FakeLocalServices(),
        FakeControlPlaneClient(),
        directory_picker=picker,
    )

    with pytest.raises(ManagementServiceError, match="picker failed") as raised:
        await service.choose_directory()
    assert raised.value.status_code == 501


@pytest.mark.asyncio
async def test_starting_main_web_starts_control_plane_dependency_first(tmp_path: Path) -> None:
    service, local, _ = _service(tmp_path)
    started = await service.start_service("web-v3", expected_epoch=0)

    assert started.status == "running"
    assert local.events == ["start:control-plane", "start:web-v3"]


@pytest.mark.asyncio
async def test_starting_delegated_service_starts_control_plane_then_forwards_epoch(
    tmp_path: Path,
) -> None:
    service, local, control_plane = _service(tmp_path)
    started = await service.start_service("a2a-node", expected_epoch=0)

    assert started.status == "running"
    assert started.epoch == 1
    assert local.events == ["start:control-plane"]
    assert control_plane.events == ["start:a2a-node"]


@pytest.mark.asyncio
async def test_stale_control_plane_stop_is_fenced_before_dependants_change(tmp_path: Path) -> None:
    service, local, control_plane = _service(tmp_path)
    local.set_running("control-plane", epoch=2)
    local.set_running("web-v3")
    control_plane.set_running("a2a-node")

    with pytest.raises(ManagementServiceError, match="stale"):
        await service.stop_service("control-plane", expected_epoch=1)

    assert local.events == []
    assert control_plane.events == []


@pytest.mark.asyncio
async def test_stopping_control_plane_stops_delegated_and_web_services_first(
    tmp_path: Path,
) -> None:
    service, local, control_plane = _service(tmp_path)
    local.set_running("control-plane")
    local.set_running("web-v3")
    control_plane.set_running("a2a-node")
    control_plane.set_running("a2a-agent-host")

    stopped = await service.stop_service("control-plane", expected_epoch=1)

    assert stopped.status == "stopped"
    assert control_plane.events == ["stop:a2a-agent-host", "stop:a2a-node"]
    assert local.events == ["stop:web-v3", "stop:control-plane"]


@pytest.mark.asyncio
async def test_all_group_starts_core_and_all_delegated_services(tmp_path: Path) -> None:
    service, local, control_plane = _service(tmp_path)
    result = await service.change_group("all", "start")

    assert result.group_id == "all"
    assert local.events == ["start:control-plane", "start:web-v3"]
    assert control_plane.events == ["start:a2a-node", "start:a2a-agent-host"]
    assert all(
        item.status == "running"
        for item in result.services
        if item.service_id in {"control-plane", "web-v3", "a2a-node", "a2a-agent-host"}
    )


@pytest.mark.asyncio
async def test_close_stops_all_services_in_dependency_order(tmp_path: Path) -> None:
    service, local, control_plane = _service(tmp_path)
    await service.start()
    await service.change_group("all", "start")
    local.events.clear()
    control_plane.events.clear()

    await service.close()

    assert control_plane.events == ["stop:a2a-agent-host", "stop:a2a-node"]
    assert local.events == ["stop:web-v3", "stop:control-plane"]
    assert not local.started
