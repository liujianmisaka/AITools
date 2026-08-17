from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from multi_agent_v2.packages.domain.json_types import JsonObject


class HumanInteractionError(ValueError):
    code = "human_interaction.invalid"


class HumanInteractionConflict(HumanInteractionError):
    code = "human_interaction.conflict"


class QuestionChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    choice_id: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")
    label: str = Field(min_length=1, max_length=256)
    detail: str | None = Field(default=None, max_length=4096)


class HumanQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._:-]+$")
    header: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=4096)
    details: str | None = Field(default=None, max_length=16_384)
    kind: Literal["single_select", "multi_select", "free_text"]
    presentation_intent: Literal["decision", "confirmation", "input", "warning"] = "input"
    choices: tuple[QuestionChoice, ...] = ()
    required: bool = True
    minimum_selections: int = Field(default=0, ge=0, le=100)
    maximum_selections: int = Field(default=1, ge=1, le=100)
    maximum_text_length: int = Field(default=4096, ge=1, le=65_536)

    @model_validator(mode="after")
    def validate_kind_contract(self) -> HumanQuestion:
        choice_ids = [choice.choice_id for choice in self.choices]
        if len(choice_ids) != len(set(choice_ids)):
            raise ValueError("question choice IDs must be unique")
        if self.kind == "free_text":
            if self.choices:
                raise ValueError("free-text questions cannot define choices")
            if self.minimum_selections != 0 or self.maximum_selections != 1:
                raise ValueError("free-text questions cannot define selection limits")
            return self
        if not self.choices:
            raise ValueError("selection questions require choices")
        if self.kind == "single_select":
            if self.minimum_selections not in {0, 1} or self.maximum_selections != 1:
                raise ValueError("single-select questions require a maximum of one selection")
        elif self.maximum_selections > len(self.choices):
            raise ValueError("maximum selections cannot exceed the number of choices")
        if self.minimum_selections > self.maximum_selections:
            raise ValueError("minimum selections cannot exceed maximum selections")
        return self


class QuestionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._:-]+$")
    workflow_instance_id: str = Field(min_length=1, max_length=128)
    activation: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=256)
    questions: tuple[HumanQuestion, ...] = Field(min_length=1, max_length=50)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def question_ids_are_unique(self) -> QuestionBatch:
        identifiers = [question.question_id for question in self.questions]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("question IDs must be unique within a batch")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("question batch expiry must include a timezone")
        return self


class HumanAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str = Field(min_length=1, max_length=128)
    selected_choice_ids: tuple[str, ...] = ()
    text: str | None = None


class HumanAnswerBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str = Field(min_length=1, max_length=128)
    batch_id: str = Field(min_length=1, max_length=128)
    answers: tuple[HumanAnswer, ...] = ()
    cancelled: bool = False
    cancellation_reason: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def validate_cancellation(self) -> HumanAnswerBatch:
        if self.cancelled and self.answers:
            raise ValueError("a cancelled answer batch cannot contain answers")
        if not self.cancelled and self.cancellation_reason is not None:
            raise ValueError("cancellation reason requires cancelled=true")
        return self


class DurableHumanCommand(BaseModel):
    """A command that must be delivered through a durable Temporal boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str
    workflow_instance_id: str
    transport: Literal["temporal_update", "temporal_signal"]
    command_name: str = Field(min_length=1, max_length=256)
    payload: JsonObject
