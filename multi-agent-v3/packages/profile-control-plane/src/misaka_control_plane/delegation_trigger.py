from __future__ import annotations

import hashlib
import json
from typing import Any

from misaka_control_plane.models import (
    DelegationSubmission,
    DelegationTriggerSubmission,
)

TRIGGER_EVENT_INPUT_KEY = "trigger_event"


def delegation_submission_from_trigger(
    submission: DelegationTriggerSubmission,
) -> DelegationSubmission:
    """Map one event route and event identity to one durable delegation request."""

    target = submission.delegation
    if TRIGGER_EVENT_INPUT_KEY in target.input:
        raise ValueError(
            f"delegation.input.{TRIGGER_EVENT_INPUT_KEY} is owned by the trigger adapter"
        )

    digest = _trigger_identity_digest(submission)
    event_payload: dict[str, Any] = submission.event.model_dump(
        mode="json",
        exclude_none=True,
    )
    event_payload = {
        "trigger_id": submission.trigger_id,
        **event_payload,
    }
    payload = target.model_dump(mode="python")
    payload.update(
        {
            "delegation_id": f"event-delegation-{digest}",
            "idempotency_key": f"event-delegation:{digest}",
            "input": {
                **target.input,
                TRIGGER_EVENT_INPUT_KEY: event_payload,
            },
        }
    )
    return DelegationSubmission.model_validate(payload)


def _trigger_identity_digest(submission: DelegationTriggerSubmission) -> str:
    identity = json.dumps(
        [
            submission.trigger_id,
            submission.event.source,
            submission.event.event_id,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
