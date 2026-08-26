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
    STEERING = "steering"
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
        if not self.operations:
            raise ContractError(
                "capability.operations_empty",
                "capability must declare at least one operation",
            )
        names = [operation.name for operation in self.operations]
        if len(names) != len(set(names)):
            raise ContractError(
                "capability.operation_duplicate",
                "capability operations must be unique",
            )
        if any(not requirement.strip() for requirement in self.resource_requirements):
            raise ContractError(
                "capability.resource_requirement_empty",
                "resource requirements must not be empty",
            )
        if len(self.resource_requirements) != len(set(self.resource_requirements)):
            raise ContractError(
                "capability.resource_requirement_duplicate",
                "resource requirements must be unique",
            )


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    """Provider-neutral model metadata exposed to composition hosts."""

    model_id: str
    display_name: str
    description: str = ""
    supported_efforts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ContractError("model.id_empty", "model id must not be empty")
        if not self.display_name.strip():
            raise ContractError("model.display_name_empty", "model display name must not be empty")
        if any(not effort.strip() for effort in self.supported_efforts):
            raise ContractError("model.effort_empty", "model efforts must not be empty")
        if len(self.supported_efforts) != len(set(self.supported_efforts)):
            raise ContractError("model.effort_duplicate", "model efforts must be unique")


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    """A provider's read-only model directory."""

    provider_id: str
    models: tuple[ModelDescriptor, ...]

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ContractError("model.provider_empty", "model provider id must not be empty")
        ids = [model.model_id for model in self.models]
        if len(ids) != len(set(ids)):
            raise ContractError("model.duplicate", "model ids must be unique per provider")
