from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from misaka_kernel_contracts.errors import ContractError
from misaka_kernel_contracts.events import JsonObject


class CapabilityFeature(StrEnum):
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"
    CANCELLATION = "cancellation"
    RESUME = "resume"
    ARTIFACTS = "artifacts"


@dataclass(frozen=True, slots=True)
class CapabilityOperation:
    name: str
    input_schema: JsonObject = field(default_factory=dict)
    output_schema: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ContractError(
                "capability.operation_empty",
                "capability operation name must not be empty",
            )


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    capability_id: str
    version: str
    operations: tuple[CapabilityOperation, ...]
    features: frozenset[CapabilityFeature] = frozenset()
    resource_requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.capability_id.strip():
            raise ContractError("capability.id_empty", "capability id must not be empty")
        if not self.version.strip():
            raise ContractError("capability.version_empty", "capability version must not be empty")
        names = [operation.name for operation in self.operations]
        if len(names) != len(set(names)):
            raise ContractError(
                "capability.operation_duplicate",
                "capability operations must be unique",
            )
