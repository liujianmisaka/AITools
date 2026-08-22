from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ProfileName = Literal["fake", "codex"]


@dataclass(frozen=True, slots=True)
class ManagementConfig:
    root: Path
    profile: ProfileName = "fake"
    management_host: str = "127.0.0.1"
    management_port: int = 8014
    service_web_port: int = 5174
    control_plane_port: int = 8016
    main_web_port: int = 5173
    codex_home: Path | None = None
    workspace_roots: tuple[Path, ...] = ()
    workspace_ids: tuple[str, ...] = ()
    state_path: Path | None = None
    provider_id: str = "codex"
    network_deny_enforced: bool = False

    def __post_init__(self) -> None:
        root = self.root.resolve()
        object.__setattr__(self, "root", root)
        if self.profile not in {"fake", "codex"}:
            raise ValueError("profile must be fake or codex")
        if not self.management_host.strip():
            raise ValueError("management host must not be empty")
        ports = {
            "management": self.management_port,
            "service web": self.service_web_port,
            "control plane": self.control_plane_port,
            "main web": self.main_web_port,
        }
        for name, port in ports.items():
            if not 1 <= port <= 65535:
                raise ValueError(f"{name} port must be between 1 and 65535")
        if len(set(ports.values())) != len(ports):
            raise ValueError(
                "management, service web, control plane, and main web ports must differ"
            )
        if not self.provider_id.strip():
            raise ValueError("provider id must not be empty")

        roots = tuple(path.resolve() for path in self.workspace_roots)
        object.__setattr__(self, "workspace_roots", roots)
        if self.workspace_ids and len(self.workspace_ids) != len(roots):
            raise ValueError("workspace ids must match workspace roots one-to-one")
        if any(not workspace_id.strip() for workspace_id in self.workspace_ids):
            raise ValueError("workspace ids must not be empty")
        if len(set(self.workspace_ids)) != len(self.workspace_ids):
            raise ValueError("workspace ids must be unique")

        if self.profile == "codex":
            if self.codex_home is None:
                raise ValueError("codex home is required for the codex profile")
            if not roots:
                raise ValueError("at least one workspace root is required for the codex profile")
            object.__setattr__(self, "codex_home", self.codex_home.resolve())

        selected_state_path = self.state_path or (
            root / ".data" / "multi-agent-v3" / f"control-plane-{self.profile}.jsonl"
        )
        object.__setattr__(self, "state_path", selected_state_path.resolve())

    @property
    def management_url(self) -> str:
        return f"http://{self.management_host}:{self.management_port}"

    @property
    def service_web_url(self) -> str:
        return f"http://127.0.0.1:{self.service_web_port}"

    @property
    def control_plane_url(self) -> str:
        return f"http://127.0.0.1:{self.control_plane_port}"

    @property
    def main_web_url(self) -> str:
        return f"http://127.0.0.1:{self.main_web_port}"

    @property
    def resolved_workspace_ids(self) -> tuple[str, ...]:
        if self.workspace_ids:
            return self.workspace_ids
        return tuple(f"workspace-{index}" for index in range(1, len(self.workspace_roots) + 1))
