from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ServiceStatus = Literal[
    "stopped",
    "starting",
    "running",
    "stopping",
    "failed",
    "unavailable",
    "on_demand",
]
ServiceScope = Literal["aitools", "control_plane", "client"]
LaunchMode = Literal["managed", "delegated", "on_demand"]


class ManagedServiceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: str
    display_name: str
    description: str
    category: str
    scope: ServiceScope
    launch_mode: LaunchMode
    status: ServiceStatus
    controllable: bool
    available: bool = True
    endpoint: str | None = None
    pid: int | None = None
    process_create_time: float | None = None
    epoch: int = 0
    started_at: str | None = None
    stopped_at: str | None = None
    exit_code: int | None = None
    last_error: str | None = None
    recent_output: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class ManagementConfigurationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str
    management_url: str
    service_web_url: str
    control_plane_url: str
    main_web_url: str
    workspace_ids: list[str]


class ServiceCollectionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    services: list[ManagedServiceView]


class GroupActionView(ServiceCollectionView):
    group_id: Literal["core", "all"]
    action: Literal["start", "stop"]


class ControlPlaneServicePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service_id: str
    display_name: str
    description: str
    category: str
    status: Literal["stopped", "starting", "running", "stopping", "failed"]
    controllable: bool
    endpoint: str | None = None
    pid: int | None = None
    process_create_time: float | None = None
    epoch: int = 0
    started_at: str | None = None
    stopped_at: str | None = None
    exit_code: int | None = None
    last_error: str | None = None
    recent_output: list[str] = Field(default_factory=list)
