from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from multi_agent_v2.packages.domain.json_types import JsonObject
from multi_agent_v2.packages.human_interaction.models import (
    DurableHumanCommand,
    HumanAnswer,
    HumanAnswerBatch,
    HumanInteractionConflict,
    HumanQuestion,
    QuestionBatch,
)


class DurableHumanCommandSink(Protocol):
    async def submit(self, command: DurableHumanCommand) -> None: ...


class HumanInteractionService:
    """Validates UI answers and hands them to a durable Temporal command sink."""

    def __init__(self, sink: DurableHumanCommandSink) -> None:
        self._sink = sink

    async def submit_answers(
        self,
        question_batch: QuestionBatch,
        answer_batch: HumanAnswerBatch,
        *,
        transport: Literal["temporal_update", "temporal_signal"] = "temporal_update",
        command_name: str = "human.answer.v1",
    ) -> DurableHumanCommand:
        command = build_durable_command(
            question_batch,
            answer_batch,
            transport=transport,
            command_name=command_name,
        )
        await self._sink.submit(command)
        return command


def build_durable_command(
    question_batch: QuestionBatch,
    answer_batch: HumanAnswerBatch,
    *,
    transport: Literal["temporal_update", "temporal_signal"] = "temporal_update",
    command_name: str = "human.answer.v1",
    now: datetime | None = None,
) -> DurableHumanCommand:
    if answer_batch.batch_id != question_batch.batch_id:
        raise HumanInteractionConflict("answer batch does not match the question batch")
    if question_batch.expires_at is not None:
        observed = now or datetime.now(UTC)
        if observed.tzinfo is None:
            raise ValueError("human interaction clock must include a timezone")
        if observed >= question_batch.expires_at:
            raise HumanInteractionConflict("question batch has expired")
    if answer_batch.cancelled:
        payload = cast(
            JsonObject,
            {
                "commandId": answer_batch.command_id,
                "batchId": question_batch.batch_id,
                "activation": question_batch.activation,
                "cancelled": True,
                "reason": answer_batch.cancellation_reason,
            },
        )
    else:
        answers_by_id = {answer.question_id: answer for answer in answer_batch.answers}
        if len(answers_by_id) != len(answer_batch.answers):
            raise HumanInteractionConflict("each question may be answered only once")
        expected = {question.question_id for question in question_batch.questions}
        if unexpected := set(answers_by_id) - expected:
            raise HumanInteractionConflict(f"answers contain unknown question IDs: {unexpected}")
        normalized = [
            _validate_answer(question, answers_by_id.get(question.question_id))
            for question in question_batch.questions
        ]
        payload = cast(
            JsonObject,
            {
                "commandId": answer_batch.command_id,
                "batchId": question_batch.batch_id,
                "activation": question_batch.activation,
                "cancelled": False,
                "answers": normalized,
            },
        )
    return DurableHumanCommand(
        command_id=answer_batch.command_id,
        workflow_instance_id=question_batch.workflow_instance_id,
        transport=transport,
        command_name=command_name,
        payload=payload,
    )


def _validate_answer(question: HumanQuestion, answer: HumanAnswer | None) -> dict[str, object]:
    if answer is None:
        if question.required:
            raise HumanInteractionConflict(
                f"required question '{question.question_id}' is unanswered"
            )
        return {"questionId": question.question_id, "skipped": True}
    if question.kind == "free_text":
        if answer.selected_choice_ids:
            raise HumanInteractionConflict("free-text answers cannot select choices")
        text = (answer.text or "").strip()
        if question.required and not text:
            raise HumanInteractionConflict("required free-text answer is blank")
        if len(text) > question.maximum_text_length:
            raise HumanInteractionConflict("free-text answer exceeds its maximum length")
        return {"questionId": question.question_id, "text": text}
    if answer.text is not None:
        raise HumanInteractionConflict("selection answers cannot contain free text")
    selected = answer.selected_choice_ids
    if len(selected) != len(set(selected)):
        raise HumanInteractionConflict("selected choice IDs must be unique")
    valid = {choice.choice_id for choice in question.choices}
    if not set(selected) <= valid:
        raise HumanInteractionConflict("answer contains an unknown choice ID")
    minimum = max(question.minimum_selections, 1 if question.required else 0)
    if len(selected) < minimum or len(selected) > question.maximum_selections:
        raise HumanInteractionConflict("answer violates the question selection limits")
    return {"questionId": question.question_id, "selectedChoiceIds": list(selected)}
