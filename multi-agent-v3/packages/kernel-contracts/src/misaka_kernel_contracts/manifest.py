from __future__ import annotations

from dataclasses import dataclass, field
from typing import NewType

from misaka_kernel_contracts.errors import ContractError

ModuleId = NewType("ModuleId", str)
ServiceKey = NewType("ServiceKey", str)


@dataclass(frozen=True, slots=True)
class ServiceRequirement:
    key: ServiceKey
    version: str | None = None
    optional: bool = False

    def __post_init__(self) -> None:
        if not str(self.key).strip():
            raise ContractError("service.key_empty", "service requirement key must not be empty")


@dataclass(frozen=True, slots=True)
class ServiceProvision:
    key: ServiceKey
    version: str
    name: str | None = None
    multiple: bool = False

    def __post_init__(self) -> None:
        if not str(self.key).strip():
            raise ContractError("service.key_empty", "service provision key must not be empty")
        if not self.version.strip():
            raise ContractError(
                "service.version_empty",
                "service provision version must not be empty",
            )
        if self.name is not None and not self.name.strip():
            raise ContractError("service.name_empty", "named service name must not be empty")


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    module_id: ModuleId
    version: str
    requires: tuple[ServiceRequirement, ...] = ()
    provides: tuple[ServiceProvision, ...] = ()
    conflicts: tuple[ModuleId, ...] = ()
    configuration_schema: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.module_id).strip():
            raise ContractError("module.id_empty", "module id must not be empty")
        if not self.version.strip():
            raise ContractError("module.version_empty", "module version must not be empty")
        provided = [(provision.key, provision.name) for provision in self.provides]
        if len(provided) != len(set(provided)):
            raise ContractError(
                "module.provision_duplicate",
                "module provides duplicate service binding",
            )
