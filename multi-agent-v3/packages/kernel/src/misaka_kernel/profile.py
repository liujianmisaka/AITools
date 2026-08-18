from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from misaka_kernel_contracts import JsonObject, ModuleId, ServiceKey

from misaka_kernel.configuration import validate_configuration
from misaka_kernel.errors import ModuleGraphError
from misaka_kernel.host import Host, Module

ModuleFactory = Callable[[], Module]


@dataclass(frozen=True, slots=True)
class ProfileDefinition:
    profile_id: str
    module_ids: tuple[ModuleId, ...]
    bindings: dict[ServiceKey, str] = field(default_factory=dict)
    configurations: dict[ModuleId, JsonObject] = field(default_factory=dict)

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
        unknown_configurations = set(profile.configurations) - set(profile.module_ids)
        if unknown_configurations:
            names = ", ".join(sorted(str(module_id) for module_id in unknown_configurations))
            raise ModuleGraphError(
                "profile.configuration_module_missing",
                f"profile configures modules that are not selected: {names}",
            )
        modules: list[Module] = []
        for module_id in profile.module_ids:
            module = self._factories[module_id]()
            if module.manifest.module_id != module_id:
                raise ModuleGraphError(
                    "profile.module_identity_mismatch",
                    f"factory for {module_id} returned {module.manifest.module_id}",
                )
            validate_configuration(
                module_id,
                module.manifest.configuration_schema,
                profile.configurations.get(module_id, {}),
            )
            modules.append(module)
        host = Host(
            name=profile.profile_id,
            bindings=profile.bindings,
            configurations=profile.configurations,
        )
        for module in modules:
            host.add_module(module)
        return host
