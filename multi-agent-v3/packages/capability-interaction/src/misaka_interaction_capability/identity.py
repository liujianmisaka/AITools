from __future__ import annotations

from dataclasses import replace

from misaka_interaction_contracts import InteractionMessage, InteractionMessageDraft


def message_matches_draft(
    message: InteractionMessage,
    draft: InteractionMessageDraft,
) -> bool:
    """Compare immutable message identity while ignoring assigned sequence and creation clock."""

    candidate = draft.to_message(message.sequence)
    normalized = replace(
        message,
        delivery_status=candidate.delivery_status,
        created_at=candidate.created_at,
    )
    return normalized == candidate
