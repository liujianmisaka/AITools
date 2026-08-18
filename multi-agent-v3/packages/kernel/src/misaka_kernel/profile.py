from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from misaka_kernel_contracts import ModuleId, ServiceKey

from misaka_kernel.errors import ModuleGraphError
from misaka_kernel.host import Host, Module

ModuleFactory = Callable[[], Module]


@dataclass(frozen=True, slots=True)
class ProfileDefinition:
    profile_id: str
    module_ids: tuple[ModuleId, ...]
    bindings: dict[ServiceKey, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("profile id must not be empty")
        if len(self.module_ids) != len(set(self.module_ids)):
            raise ValueError("profile module ids must be unique")


class ProfileLoader:
    def __init__(self, factories: Mapping[ModuleId, ModuleFactory]) -> None:
        self._factories = dict(factories)

    def create_host(self, profile: ProfileDefinition) -> Host:
        missing = [
            module_id for module_id in profile.module_ids if module_id not in self._factories
        ]
        if missing:
            names = ", ".join(str(module_id) for module_id in missing)
            raise ModuleGraphError(
                "profile.module_missing",
                f"profile modules unavailable: {names}",
            )
        host = Host(name=profile.profile_id, bindings=profile.bindings)
        for module_id in profile.module_ids:
            host.add_module(self._factories[module_id]())
        return host
