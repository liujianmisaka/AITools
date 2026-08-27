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
ClaudeRuntimeMode = Literal["native", "opencodex"]
CoordinatorReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]


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


class ProviderConfigurationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    kind: Literal["fake", "codex", "claude"]
    codex_home: str | None
    config_overrides: list[str]
    claude_config_dir: str | None = None
    claude_cli_path: str | None = None
    model_ids: list[str] = Field(default_factory=list)
    network_deny_enforced: bool


class ManagementConfigurationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: list[ProviderConfigurationView]
    allowed_path_roots: list[str]
    claude_runtime_mode: ClaudeRuntimeMode = "native"
    claude_opencodex_base_url: str = "http://127.0.0.1:10100"
    claude_opencodex_auth_token_env: str = "ANTHROPIC_AUTH_TOKEN"
    coordinator_model: str = "pixel/gpt-5.6-luna"
    coordinator_reasoning_effort: CoordinatorReasoningEffort = "medium"
    coordinator_api_key_env: str = "OPENAI_API_KEY"
    coordinator_base_url: str | None = "http://127.0.0.1:10100/v1"
    coordinator_max_decision_steps: int = 16
    coordinator_wait_timeout_ms: int = 0
    coordinator_max_concurrent_delegations: int = 8
    coordinator_max_total_delegations: int = 30
    coordinator_max_delegation_depth: int = 3
    coordinator_max_plan_revisions: int = 10
    coordinator_max_retries_per_node: int = 2
    coordinator_max_runtime_minutes: int = 120
    coordinator_max_model_activations: int = 50
    management_url: str
    service_web_url: str
    control_plane_url: str
    main_web_url: str
    coordinator_url: str


class ProviderConfigurationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1)
    kind: Literal["fake", "codex", "claude"]
    codex_home: str | None
    config_overrides: list[str]
    claude_config_dir: str | None = None
    claude_cli_path: str | None = None
    model_ids: list[str] = Field(default_factory=list)
    network_deny_enforced: bool


class ManagementConfigurationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: list[ProviderConfigurationUpdate] = Field(min_length=1)
    allowed_path_roots: list[str]
    claude_runtime_mode: ClaudeRuntimeMode = "native"
    claude_opencodex_base_url: str = Field(default="http://127.0.0.1:10100", min_length=1)
    claude_opencodex_auth_token_env: str = Field(default="ANTHROPIC_AUTH_TOKEN", min_length=1)
    coordinator_model: str = Field(default="pixel/gpt-5.6-luna", min_length=1)
    coordinator_reasoning_effort: CoordinatorReasoningEffort = "medium"
    coordinator_api_key_env: str = Field(default="OPENAI_API_KEY", min_length=1)
    coordinator_base_url: str | None = "http://127.0.0.1:10100/v1"
    coordinator_max_decision_steps: int = Field(default=16, ge=1, le=128)
    coordinator_wait_timeout_ms: int = Field(default=0, ge=0, le=300_000)
    coordinator_max_concurrent_delegations: int = Field(default=8, ge=1)
    coordinator_max_total_delegations: int = Field(default=30, ge=1)
    coordinator_max_delegation_depth: int = Field(default=3, ge=0)
    coordinator_max_plan_revisions: int = Field(default=10, ge=1)
    coordinator_max_retries_per_node: int = Field(default=2, ge=0)
    coordinator_max_runtime_minutes: int = Field(default=120, ge=1)
    coordinator_max_model_activations: int = Field(default=50, ge=1)


class DirectoryPickerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_path: str | None = Field(default=None, min_length=1)


class DirectoryPickerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str | None


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
