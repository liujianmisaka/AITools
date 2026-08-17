from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from multi_agent_v2.packages.human_interaction import (
    DurableHumanCommand,
    HumanAnswer,
    HumanAnswerBatch,
    HumanInteractionConflict,
    HumanInteractionService,
    HumanQuestion,
    QuestionBatch,
    QuestionChoice,
)


class _Sink:
    def __init__(self) -> None:
        self.commands: list[DurableHumanCommand] = []

    async def submit(self, command: DurableHumanCommand) -> None:
        self.commands.append(command)


def _batch() -> QuestionBatch:
    return QuestionBatch(
        batch_id="release:1",
        workflow_instance_id="workflow-1",
        activation=3,
        title="Release decision",
        questions=(
            HumanQuestion(
                question_id="target",
                header="Target",
                prompt="Choose release targets",
                kind="multi_select",
                choices=(
                    QuestionChoice(choice_id="staging", label="Staging"),
                    QuestionChoice(choice_id="production", label="Production"),
                ),
                minimum_selections=1,
                maximum_selections=2,
            ),
            HumanQuestion(
                question_id="note",
                header="Note",
                prompt="Add a release note",
                kind="free_text",
            ),
        ),
    )


async def test_answers_are_validated_then_sent_to_a_durable_command_sink() -> None:
    sink = _Sink()
    service = HumanInteractionService(sink)
    answer = HumanAnswerBatch(
        command_id="decision-1",
        batch_id="release:1",
        answers=(
            HumanAnswer(question_id="target", selected_choice_ids=("staging", "production")),
            HumanAnswer(question_id="note", text="Ship it"),
        ),
    )

    command = await service.submit_answers(_batch(), answer)

    assert sink.commands == [command]
    assert command.transport == "temporal_update"
    assert command.payload["commandId"] == "decision-1"
    assert command.payload["activation"] == 3
    assert command.payload["answers"] == [
        {"questionId": "target", "selectedChoiceIds": ["staging", "production"]},
        {"questionId": "note", "text": "Ship it"},
    ]


async def test_invalid_answer_never_reaches_the_durable_sink() -> None:
    sink = _Sink()
    service = HumanInteractionService(sink)
    answer = HumanAnswerBatch(
        command_id="decision-2",
        batch_id="release:1",
        answers=(
            HumanAnswer(question_id="target", selected_choice_ids=("unknown",)),
            HumanAnswer(question_id="note", text="Ship it"),
        ),
    )

    with pytest.raises(HumanInteractionConflict):
        await service.submit_answers(_batch(), answer)

    assert sink.commands == []


async def test_cancellation_is_a_durable_command_not_an_in_process_future() -> None:
    sink = _Sink()
    command = await HumanInteractionService(sink).submit_answers(
        _batch(),
        HumanAnswerBatch(
            command_id="decision-3",
            batch_id="release:1",
            cancelled=True,
            cancellation_reason="operator stopped the release",
        ),
        transport="temporal_signal",
    )

    assert command.payload["cancelled"] is True
    assert command.transport == "temporal_signal"


async def test_expired_question_batch_never_reaches_the_durable_sink() -> None:
    sink = _Sink()
    batch = _batch().model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    answer = HumanAnswerBatch(
        command_id="decision-4",
        batch_id=batch.batch_id,
        answers=(
            HumanAnswer(question_id="target", selected_choice_ids=("staging",)),
            HumanAnswer(question_id="note", text="Ship it"),
        ),
    )

    with pytest.raises(HumanInteractionConflict, match="expired"):
        await HumanInteractionService(sink).submit_answers(batch, answer)

    assert sink.commands == []


def test_question_batch_expiry_requires_a_timezone() -> None:
    payload = _batch().model_dump()
    payload["expires_at"] = datetime(2026, 8, 17)

    with pytest.raises(ValidationError, match="timezone"):
        QuestionBatch.model_validate(payload)
