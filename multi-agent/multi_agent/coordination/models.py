from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from multi_agent.domain.models import IDENTIFIER_PATTERN

ContractFieldName = Annotated[str, Field(min_length=1, max_length=128)]
ReasonCode = Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]


class GatePhase(str, Enum):
    input = "input"
    output = "output"


class ContractValueType(str, Enum):
    object = "object"
    array = "array"
    string = "string"
    number = "number"
    boolean = "boolean"


class AdvisorAction(str, Enum):
    admit = "admit"
    reject = "reject"
    revise = "revise"


class AdmissionDecision(str, Enum):
    admitted = "admitted"
    rejected = "rejected"


class DataContract(BaseModel):
    """Small, executable contract enforced by deterministic application code."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=IDENTIFIER_PATTERN)
    value_type: ContractValueType
    required_fields: list[ContractFieldName] = Field(default_factory=list, max_length=64)
    allowed_fields: list[ContractFieldName] | None = Field(default=None, max_length=128)
    max_serialized_bytes: int = Field(default=65_536, ge=1, le=1_048_576)
    instructions: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def validate_object_fields(self) -> "DataContract":
        if self.value_type != ContractValueType.object:
            if self.required_fields or self.allowed_fields is not None:
                raise ValueError(
                    "required_fields and allowed_fields are only valid for object contracts"
                )
            return self
        if len(self.required_fields) != len(set(self.required_fields)):
            raise ValueError("required_fields contains duplicates")
        if self.allowed_fields is not None:
            if len(self.allowed_fields) != len(set(self.allowed_fields)):
                raise ValueError("allowed_fields contains duplicates")
            missing = set(self.required_fields) - set(self.allowed_fields)
            if missing:
                raise ValueError(
                    f"required_fields must be allowed: {sorted(missing)}"
                )
        return self


class CandidateStep(BaseModel):
    """A server-owned continuation choice; never an executable task specification."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=IDENTIFIER_PATTERN)
    description: str = Field(min_length=1, max_length=1_000)


class ContractCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: GatePhase
    contract: DataContract
    value: Any
    context_summary: str | None = Field(default=None, max_length=10_000)
    candidate_next_steps: list[CandidateStep] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_candidates(self) -> "ContractCheckRequest":
        ids = [item.id for item in self.candidate_next_steps]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate_next_steps contains duplicate IDs")
        return self


class AdvisorDraft(BaseModel):
    """The complete and deliberately narrow output Pi is allowed to author."""

    model_config = ConfigDict(extra="forbid")

    action: AdvisorAction
    reason_codes: list[ReasonCode] = Field(min_length=1, max_length=16)
    explanation: str = Field(min_length=1, max_length=2_000)
    normalized_value: Any | None = None
    recommended_next_step_ids: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_action_payload(self) -> "AdvisorDraft":
        if self.action == AdvisorAction.revise and self.normalized_value is None:
            raise ValueError("revise requires normalized_value")
        if self.action != AdvisorAction.revise and self.normalized_value is not None:
            raise ValueError("normalized_value is only valid for revise")
        if self.action == AdvisorAction.reject and self.recommended_next_step_ids:
            raise ValueError("reject cannot recommend a next step")
        if len(self.recommended_next_step_ids) != len(
            set(self.recommended_next_step_ids)
        ):
            raise ValueError("recommended_next_step_ids contains duplicates")
        return self


class AdvisorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: AdvisorDraft
    advisor_session_id: str | None = Field(default=None, max_length=500)
    event_count: int = Field(default=0, ge=0)


class ContractCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    advisor: str
    advisor_session_id: str | None = None
    event_count: int = Field(default=0, ge=0)
    phase: GatePhase
    advisor_action: AdvisorAction
    decision: AdmissionDecision
    adjusted: bool
    effective_value: Any | None = None
    reason_codes: list[str]
    explanation: str
    recommended_next_step_ids: list[str]
    contract_violations: list[str]
