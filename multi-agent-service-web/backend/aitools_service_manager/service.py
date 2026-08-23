from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from misaka_service_runtime import (
    ManagedServiceStatus,
    ServiceManager,
    ServiceSnapshot,
)

from aitools_service_manager.catalog import (
    CONTROL_PLANE_SERVICE_ID,
    MAIN_WEB_SERVICE_ID,
)
from aitools_service_manager.client import (
    ControlPlaneClient,
    ControlPlaneRequestError,
)
from aitools_service_manager.config import (
    ManagementConfig,
    ProviderConfiguration,
    RuntimeConfiguration,
    RuntimeConfigurationStore,
)
from aitools_service_manager.directory_picker import (
    DirectoryPicker,
    DirectoryPickerError,
    NativeDirectoryPicker,
)
from aitools_service_manager.models import (
    ControlPlaneServicePayload,
    GroupActionView,
    ManagedServiceView,
    ManagementConfigurationUpdate,
    ManagementConfigurationView,
    ProviderConfigurationView,
)

MCP_SERVICE_ID = "multi-agent-mcp"
GroupId = Literal["core", "all"]
GroupAction = Literal["start", "stop"]


class LocalServiceManager(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def list(self) -> tuple[ServiceSnapshot, ...]: ...

    async def get(self, service_id: str) -> ServiceSnapshot: ...

    async def start_service(
        self, service_id: str, *, expected_epoch: int | None = None
    ) -> ServiceSnapshot: ...

    async def stop(
        self, service_id: str, *, expected_epoch: int | None = None
    ) -> ServiceSnapshot: ...


class ManagementServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class DelegatedServiceDescriptor:
    service_id: str
    display_name: str
    description: str
    category: str
    endpoint: str


DELEGATED_SERVICES = (
    DelegatedServiceDescriptor(
        service_id="a2a-node",
        display_name="Standalone A2A Node",
        description="独立的 A2A 协议节点; 由 Control Plane 的静态目录管理。",
        category="A2A",
        endpoint="http://127.0.0.1:8025",
    ),
    DelegatedServiceDescriptor(
        service_id="a2a-agent-host",
        display_name="A2A Agent Host",
        description="通过 A2A 发布本地 Agent Host; 由 Control Plane 管理。",
        category="Agent",
        endpoint="http://127.0.0.1:8026",
    ),
)


class ManagementService:
    def __init__(
        self,
        config: ManagementConfig,
        local_services: LocalServiceManager | ServiceManager,
        control_plane: ControlPlaneClient,
        configuration_store: RuntimeConfigurationStore | None = None,
        directory_picker: DirectoryPicker | None = None,
    ) -> None:
        self._config = config
        self._local_services = local_services
        self._control_plane = control_plane
        self._directory_picker = directory_picker or NativeDirectoryPicker()
        self._operation_lock = asyncio.Lock()
        configuration_path = config.configuration_path
        if configuration_path is None:
            raise ValueError("runtime configuration path was not resolved")
        self._configuration_store = configuration_store or RuntimeConfigurationStore(
            configuration_path
        )
        self._runtime_configuration = self._configuration_store.load_or_create(
            config.initial_runtime_configuration
        )

    async def start(self) -> None:
        await self._local_services.start()

    async def close(self) -> None:
        async with self._operation_lock:
            try:
                await self._stop_group("all")
            finally:
                await self._local_services.close()

    def configuration(self) -> ManagementConfigurationView:
        runtime = self._runtime_configuration
        return ManagementConfigurationView(
            providers=[
                ProviderConfigurationView(
                    provider_id=provider.provider_id,
                    kind=provider.kind,
                    codex_home=(
                        str(provider.codex_home) if provider.codex_home is not None else None
                    ),
                    config_overrides=list(provider.config_overrides),
                    claude_config_dir=(
                        str(provider.claude_config_dir)
                        if provider.claude_config_dir is not None
                        else None
                    ),
                    claude_cli_path=(
                        str(provider.claude_cli_path)
                        if provider.claude_cli_path is not None
                        else None
                    ),
                    model_ids=list(provider.model_ids),
                    network_deny_enforced=provider.network_deny_enforced,
                )
                for provider in runtime.providers
            ],
            allowed_path_roots=[str(path) for path in runtime.allowed_path_roots],
            management_url=self._config.management_url,
            service_web_url=self._config.service_web_url,
            control_plane_url=self._config.control_plane_url,
            main_web_url=self._config.main_web_url,
        )

    async def choose_directory(self, initial_path: str | None = None) -> Path | None:
        selected_initial_path = Path(initial_path) if initial_path is not None else None
        try:
            return await asyncio.to_thread(
                self._directory_picker.choose,
                selected_initial_path,
            )
        except DirectoryPickerError as exc:
            raise ManagementServiceError(
                "directory_picker.unavailable",
                str(exc),
                status_code=501,
            ) from exc

    async def update_configuration(
        self,
        submission: ManagementConfigurationUpdate,
    ) -> ManagementConfigurationView:
        async with self._operation_lock:
            control_plane = await self._local_services.get(CONTROL_PLANE_SERVICE_ID)
            if control_plane.status not in {
                ManagedServiceStatus.STOPPED,
                ManagedServiceStatus.FAILED,
            }:
                raise ManagementServiceError(
                    "configuration.control_plane_running",
                    "stop the core services before changing Control Plane configuration",
                )
            try:
                configuration = RuntimeConfiguration(
                    providers=tuple(
                        ProviderConfiguration(
                            provider_id=provider.provider_id,
                            kind=provider.kind,
                            codex_home=(
                                Path(provider.codex_home)
                                if provider.codex_home is not None
                                else None
                            ),
                            config_overrides=tuple(provider.config_overrides),
                            claude_config_dir=(
                                Path(provider.claude_config_dir)
                                if provider.claude_config_dir is not None
                                else None
                            ),
                            claude_cli_path=(
                                Path(provider.claude_cli_path)
                                if provider.claude_cli_path is not None
                                else None
                            ),
                            model_ids=tuple(provider.model_ids),
                            network_deny_enforced=provider.network_deny_enforced,
                        )
                        for provider in submission.providers
                    ),
                    allowed_path_roots=tuple(Path(path) for path in submission.allowed_path_roots),
                )
                self._configuration_store.save(configuration)
            except ValueError as exc:
                raise ManagementServiceError(
                    "configuration.invalid",
                    str(exc),
                    status_code=422,
                ) from exc
            except OSError as exc:
                raise ManagementServiceError(
                    "configuration.persist_failed",
                    f"runtime configuration could not be saved: {exc}",
                    status_code=500,
                ) from exc
            self._runtime_configuration = configuration
            return self.configuration()

    async def services(self) -> list[ManagedServiceView]:
        local_snapshots = await self._local_services.list()
        local_views = {
            snapshot.service_id: _local_service_view(snapshot) for snapshot in local_snapshots
        }
        control_plane = local_views[CONTROL_PLANE_SERVICE_ID]
        delegated = await self._delegated_views(control_plane.status == "running")
        delegated_by_id = {service.service_id: service for service in delegated}

        ordered = [
            local_views[CONTROL_PLANE_SERVICE_ID],
            local_views[MAIN_WEB_SERVICE_ID],
        ]
        for descriptor in DELEGATED_SERVICES:
            ordered.append(delegated_by_id.pop(descriptor.service_id))
        ordered.extend(sorted(delegated_by_id.values(), key=lambda service: service.service_id))
        ordered.append(_mcp_service_view())
        return ordered

    async def service(self, service_id: str) -> ManagedServiceView:
        for service in await self.services():
            if service.service_id == service_id:
                return service
        raise ManagementServiceError(
            "service.not_found",
            f"service {service_id} is not registered",
            status_code=404,
        )

    async def start_service(self, service_id: str, *, expected_epoch: int) -> ManagedServiceView:
        async with self._operation_lock:
            return await self._start_service(service_id, expected_epoch=expected_epoch)

    async def stop_service(self, service_id: str, *, expected_epoch: int) -> ManagedServiceView:
        async with self._operation_lock:
            return await self._stop_service(service_id, expected_epoch=expected_epoch)

    async def change_group(self, group_id: GroupId, action: GroupAction) -> GroupActionView:
        async with self._operation_lock:
            if action == "start":
                await self._start_group(group_id)
            else:
                await self._stop_group(group_id)
            return GroupActionView(
                group_id=group_id,
                action=action,
                services=await self.services(),
            )

    async def _start_service(self, service_id: str, *, expected_epoch: int) -> ManagedServiceView:
        if service_id == CONTROL_PLANE_SERVICE_ID:
            return _local_service_view(
                await self._local_services.start_service(
                    service_id,
                    expected_epoch=expected_epoch,
                )
            )
        if service_id == MAIN_WEB_SERVICE_ID:
            current = await self._local_services.get(service_id)
            _require_epoch(current, expected_epoch)
            await self._ensure_control_plane()
            return _local_service_view(
                await self._local_services.start_service(
                    service_id,
                    expected_epoch=expected_epoch,
                )
            )
        if service_id == MCP_SERVICE_ID:
            raise ManagementServiceError(
                "service.on_demand",
                "MCP gateway is started on demand by the configured client",
            )

        await self._ensure_control_plane()
        payload = await self._control_plane.start_service(
            service_id,
            expected_epoch=expected_epoch,
        )
        return _delegated_service_view(payload)

    async def _stop_service(self, service_id: str, *, expected_epoch: int) -> ManagedServiceView:
        if service_id == CONTROL_PLANE_SERVICE_ID:
            current = await self._local_services.get(service_id)
            _require_epoch(current, expected_epoch)
            await self._stop_control_plane_dependants()
            return _local_service_view(
                await self._local_services.stop(
                    service_id,
                    expected_epoch=expected_epoch,
                )
            )
        if service_id == MAIN_WEB_SERVICE_ID:
            return _local_service_view(
                await self._local_services.stop(
                    service_id,
                    expected_epoch=expected_epoch,
                )
            )
        if service_id == MCP_SERVICE_ID:
            raise ManagementServiceError(
                "service.on_demand",
                "MCP gateway lifecycle belongs to the configured client",
            )

        control_plane = await self._local_services.get(CONTROL_PLANE_SERVICE_ID)
        if control_plane.status is not ManagedServiceStatus.RUNNING:
            raise ManagementServiceError(
                "service.dependency_unavailable",
                "Control Plane must be running before a delegated service can be stopped",
            )
        payload = await self._control_plane.stop_service(
            service_id,
            expected_epoch=expected_epoch,
        )
        return _delegated_service_view(payload)

    async def _start_group(self, group_id: GroupId) -> None:
        await self._ensure_control_plane()
        web = await self._local_services.get(MAIN_WEB_SERVICE_ID)
        await self._local_services.start_service(MAIN_WEB_SERVICE_ID, expected_epoch=web.epoch)
        if group_id == "all":
            for service in await self._control_plane.list_services():
                if service.controllable:
                    await self._control_plane.start_service(
                        service.service_id,
                        expected_epoch=service.epoch,
                    )

    async def _stop_group(self, group_id: GroupId) -> None:
        # Both groups contain Control Plane. Its delegated services must stop first
        # even when the caller only requested the core group.
        del group_id
        await self._stop_control_plane_dependants()
        control_plane = await self._local_services.get(CONTROL_PLANE_SERVICE_ID)
        await self._local_services.stop(
            CONTROL_PLANE_SERVICE_ID,
            expected_epoch=control_plane.epoch,
        )

    async def _ensure_control_plane(self) -> ServiceSnapshot:
        current = await self._local_services.get(CONTROL_PLANE_SERVICE_ID)
        if current.status is ManagedServiceStatus.RUNNING:
            return current
        return await self._local_services.start_service(
            CONTROL_PLANE_SERVICE_ID,
            expected_epoch=current.epoch,
        )

    async def _stop_control_plane_dependants(self) -> None:
        control_plane = await self._local_services.get(CONTROL_PLANE_SERVICE_ID)
        if control_plane.status is ManagedServiceStatus.RUNNING:
            with suppress(ControlPlaneRequestError):
                delegated = await self._control_plane.list_services()
                for service in reversed(delegated):
                    if service.controllable:
                        with suppress(ControlPlaneRequestError):
                            await self._control_plane.stop_service(
                                service.service_id,
                                expected_epoch=service.epoch,
                            )
        web = await self._local_services.get(MAIN_WEB_SERVICE_ID)
        await self._local_services.stop(MAIN_WEB_SERVICE_ID, expected_epoch=web.epoch)

    async def _delegated_views(self, control_plane_running: bool) -> list[ManagedServiceView]:
        if not control_plane_running:
            return [_delegated_placeholder(descriptor) for descriptor in DELEGATED_SERVICES]
        try:
            payloads = await self._control_plane.list_services()
        except ControlPlaneRequestError as exc:
            return [
                _delegated_placeholder(descriptor, error_message=str(exc))
                for descriptor in DELEGATED_SERVICES
            ]

        views = [_delegated_service_view(payload) for payload in payloads]
        received = {view.service_id for view in views}
        views.extend(
            _delegated_placeholder(
                descriptor,
                error_message="Control Plane did not publish this configured service",
            )
            for descriptor in DELEGATED_SERVICES
            if descriptor.service_id not in received
        )
        return views


def _local_service_view(snapshot: ServiceSnapshot) -> ManagedServiceView:
    identity = snapshot.process_identity
    depends_on = [CONTROL_PLANE_SERVICE_ID] if snapshot.service_id == MAIN_WEB_SERVICE_ID else []
    return ManagedServiceView(
        service_id=snapshot.service_id,
        display_name=snapshot.display_name,
        description=snapshot.description,
        category=snapshot.category,
        scope="aitools",
        launch_mode="managed",
        status=snapshot.status.value,
        controllable=snapshot.controllable,
        endpoint=snapshot.endpoint,
        pid=snapshot.pid,
        process_create_time=identity.create_time if identity is not None else None,
        epoch=snapshot.epoch,
        started_at=snapshot.started_at.isoformat() if snapshot.started_at else None,
        stopped_at=snapshot.stopped_at.isoformat() if snapshot.stopped_at else None,
        exit_code=snapshot.exit_code,
        last_error=snapshot.last_error,
        recent_output=list(snapshot.recent_output),
        depends_on=depends_on,
    )


def _delegated_service_view(payload: ControlPlaneServicePayload) -> ManagedServiceView:
    return ManagedServiceView(
        **payload.model_dump(),
        scope="control_plane",
        launch_mode="delegated",
        available=True,
        depends_on=[CONTROL_PLANE_SERVICE_ID],
    )


def _delegated_placeholder(
    descriptor: DelegatedServiceDescriptor,
    *,
    error_message: str = "等待 Control Plane 启动后读取实时状态",
) -> ManagedServiceView:
    return ManagedServiceView(
        service_id=descriptor.service_id,
        display_name=descriptor.display_name,
        description=descriptor.description,
        category=descriptor.category,
        scope="control_plane",
        launch_mode="delegated",
        status="unavailable",
        controllable=True,
        available=False,
        endpoint=descriptor.endpoint,
        last_error=error_message,
        depends_on=[CONTROL_PLANE_SERVICE_ID],
    )


def _mcp_service_view() -> ManagedServiceView:
    return ManagedServiceView(
        service_id=MCP_SERVICE_ID,
        display_name="Multi-Agent V3 MCP Gateway",
        description="由 Codex 或其他 MCP 客户端按需启动的 stdio 网关; 不是共享常驻进程。",
        category="Integration",
        scope="client",
        launch_mode="on_demand",
        status="on_demand",
        controllable=False,
        available=True,
        depends_on=[CONTROL_PLANE_SERVICE_ID],
    )


def _require_epoch(snapshot: ServiceSnapshot, expected_epoch: int) -> None:
    if snapshot.epoch != expected_epoch:
        raise ManagementServiceError(
            "service.epoch_fenced",
            f"service epoch {expected_epoch} is stale; current epoch is {snapshot.epoch}",
        )
