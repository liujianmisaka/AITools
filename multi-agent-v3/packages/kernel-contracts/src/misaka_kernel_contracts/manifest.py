from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import NewType

from misaka_kernel_contracts.errors import ContractError
from misaka_kernel_contracts.events import JsonObject

ModuleId = NewType("ModuleId", str)
ServiceKey = NewType("ServiceKey", str)


class ServiceShape(StrEnum):
    SINGLETON = "singleton"
    NAMED = "named"
    SCOPED = "scoped"


@dataclass(frozen=True, slots=True)
class ServiceRequirement:
    key: ServiceKey
    version: str | None = None

    def __post_init__(self) -> None:
        if not str(self.key).strip():
            raise ContractError("service.key_empty", "service requirement key must not be empty")


@dataclass(frozen=True, slots=True)
class ServiceProvision:
    key: ServiceKey
    version: str
    shape: ServiceShape = ServiceShape.SINGLETON
    name: str | None = None

    def __post_init__(self) -> None:
        if not str(self.key).strip():
            raise ContractError("service.key_empty", "service provision key must not be empty")
        if not self.version.strip():
            raise ContractError(
                "service.version_empty",
                "service provision version must not be empty",
            )
        if self.shape is ServiceShape.NAMED:
            if self.name is None or not self.name.strip():
                raise ContractError(
                    "service.name_required",
                    "named service must declare a provider name",
                )
        elif self.name is not None:
            raise ContractError(
                "service.name_unexpected",
                "only named services may declare a provider name",
            )


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    module_id: ModuleId
    version: str
    requires: tuple[ServiceRequirement, ...] = ()
    optional_requires: tuple[ServiceRequirement, ...] = ()
    provides: tuple[ServiceProvision, ...] = ()
    conflicts: tuple[ModuleId, ...] = ()
    configuration_schema: JsonObject = field(default_factory=dict)

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
        required = [requirement.key for requirement in self.requires]
        optional = [requirement.key for requirement in self.optional_requires]
        if len(required) != len(set(required)):
            raise ContractError(
                "module.requirement_duplicate",
                "module requires a service more than once",
            )
        if len(optional) != len(set(optional)):
            raise ContractError(
                "module.optional_requirement_duplicate",
                "module optionally requires a service more than once",
            )
        if set(required) & set(optional):
            raise ContractError(
                "module.requirement_overlap",
                "module cannot require the same service as both required and optional",
            )
