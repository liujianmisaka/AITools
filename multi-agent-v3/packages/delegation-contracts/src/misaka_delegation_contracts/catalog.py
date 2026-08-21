from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType

from misaka_kernel_contracts import ContractError, JsonObject


class ContinuationOperation(StrEnum):
    PREPARE = "prepare"
    START = "start"
    FOLLOW_UP = "follow_up"
    REPLY = "reply"
    STEER = "steer"
    PAUSE = "pause"
    RESUME = "resume"
    ACK = "ack"
    CANCEL = "cancel"
    CLOSE = "close"
    RECONCILE = "reconcile"


class ContinuationActivationEffect(StrEnum):
    NONE = "none"
    PREPARE = "prepare"
    START_PREPARED = "start_prepared"
    CREATE_NEW = "create_new"
    CONTROL_EXISTING = "control_existing"
    TERMINATE_EXISTING = "terminate_existing"
    CLOSE_CHANNEL = "close_channel"
    RECONCILE_EXISTING = "reconcile_existing"


class ContinuationLeaseRequirement(StrEnum):
    NONE = "none"
    SESSION = "session"


class ContinuationConcurrencyRule(StrEnum):
    NONE = "none"
    SESSION_EXCLUSIVE = "session_exclusive"
    LIVE_ACTIVATION = "live_activation"


class ContinuationCompletionBoundary(StrEnum):
    REQUEST_ACCEPTED = "request_accepted"
    ACTIVATION_PREPARED = "activation_prepared"
    ACTIVATION_STARTED = "activation_started"
    ACTIVATION_TERMINAL = "activation_terminal"
    MESSAGE_COMPLETED = "message_completed"
    CHANNEL_CLOSED = "channel_closed"
    RECONCILIATION_RECORDED = "reconciliation_recorded"


class ContinuationRecoveryPolicy(StrEnum):
    REPLAY_SAFE = "replay_safe"
    RECONCILE_PREPARED = "reconcile_prepared"
    RECONCILE_LIVE = "reconcile_live"


@dataclass(frozen=True, slots=True)
class ContinuationOperationSpec:
    operation: ContinuationOperation
    input_schema: JsonObject
    output_schema: JsonObject
    activation_effect: ContinuationActivationEffect
    lease_requirement: ContinuationLeaseRequirement
    concurrency: ContinuationConcurrencyRule
    completion_boundary: ContinuationCompletionBoundary
    recovery_policy: ContinuationRecoveryPolicy
    requires_session: bool
    requires_channel: bool
    requires_message: bool
    requires_expected_activation: bool
    requires_reply_target: bool = False
    requires_correlation: bool = False

    def __post_init__(self) -> None:
        for name, schema in (
            ("input_schema", self.input_schema),
            ("output_schema", self.output_schema),
        ):
            schema_copy = deepcopy(schema)
            object.__setattr__(self, name, schema_copy)
            if schema_copy.get("type") != "object":
                raise ContractError(
                    "continuation.operation_schema_invalid",
                    f"{name} must be an object schema",
                )
        if (
            self.lease_requirement is ContinuationLeaseRequirement.SESSION
            and not self.requires_session
        ):
            raise ContractError(
                "continuation.operation_lease_scope_invalid",
                "session lease requirements require a session reference",
            )
        if self.requires_channel and not self.requires_session:
            raise ContractError(
                "continuation.operation_channel_scope_invalid",
                "channel operations require a session reference",
            )
        if self.requires_message and not self.requires_channel:
            raise ContractError(
                "continuation.operation_message_scope_invalid",
                "message operations require a channel reference",
            )
        if self.requires_reply_target and not self.requires_message:
            raise ContractError(
                "continuation.operation_reply_target_invalid",
                "reply target requirements require a message reference",
            )
        if self.requires_correlation and not self.requires_message:
            raise ContractError(
                "continuation.operation_correlation_scope_invalid",
                "correlation requirements require a message reference",
            )


_EMPTY_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
}
_OPEN_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": True,
}
_REASON_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"reason": {"type": "string"}},
    "additionalProperties": False,
}
_SNAPSHOT_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "required": ["delegation_id", "status", "revision", "activation_count"],
    "properties": {
        "delegation_id": {"type": "string"},
        "status": {"type": "string"},
        "revision": {"type": "integer"},
        "activation_count": {"type": "integer"},
    },
    "additionalProperties": True,
}


def _spec(
    operation: ContinuationOperation,
    *,
    input_schema: JsonObject,
    activation_effect: ContinuationActivationEffect,
    lease_requirement: ContinuationLeaseRequirement,
    concurrency: ContinuationConcurrencyRule,
    completion_boundary: ContinuationCompletionBoundary,
    recovery_policy: ContinuationRecoveryPolicy,
    requires_session: bool,
    requires_channel: bool,
    requires_message: bool,
    requires_expected_activation: bool,
    requires_reply_target: bool = False,
    requires_correlation: bool = False,
) -> ContinuationOperationSpec:
    return ContinuationOperationSpec(
        operation=operation,
        input_schema=dict(input_schema),
        output_schema=dict(_SNAPSHOT_OUTPUT_SCHEMA),
        activation_effect=activation_effect,
        lease_requirement=lease_requirement,
        concurrency=concurrency,
        completion_boundary=completion_boundary,
        recovery_policy=recovery_policy,
        requires_session=requires_session,
        requires_channel=requires_channel,
        requires_message=requires_message,
        requires_expected_activation=requires_expected_activation,
        requires_reply_target=requires_reply_target,
        requires_correlation=requires_correlation,
    )


class ContinuationOperationCatalog(Mapping[ContinuationOperation, ContinuationOperationSpec]):
    def __init__(self, catalog: Mapping[ContinuationOperation, ContinuationOperationSpec]) -> None:
        for operation, spec in catalog.items():
            if operation is not spec.operation:
                raise ContractError(
                    "continuation.operation_catalog_key_mismatch",
                    f"catalog key {operation.value} does not match spec {spec.operation.value}",
                )
        if len(catalog) != len(ContinuationOperation) or set(catalog) != set(ContinuationOperation):
            raise ContractError(
                "continuation.operation_catalog_incomplete",
                "catalog must declare every continuation operation exactly once",
            )
        self._catalog = MappingProxyType(
            {operation: replace(spec) for operation, spec in catalog.items()}
        )

    def __getitem__(self, operation: ContinuationOperation) -> ContinuationOperationSpec:
        return replace(self._catalog[operation])

    def __iter__(self) -> Iterator[ContinuationOperation]:
        return iter(self._catalog)

    def __len__(self) -> int:
        return len(self._catalog)


CONTINUATION_OPERATION_CATALOG: Mapping[ContinuationOperation, ContinuationOperationSpec] = (
    ContinuationOperationCatalog(
        {
            ContinuationOperation.PREPARE: _spec(
                ContinuationOperation.PREPARE,
                input_schema=_OPEN_INPUT_SCHEMA,
                activation_effect=ContinuationActivationEffect.PREPARE,
                lease_requirement=ContinuationLeaseRequirement.SESSION,
                concurrency=ContinuationConcurrencyRule.SESSION_EXCLUSIVE,
                completion_boundary=ContinuationCompletionBoundary.ACTIVATION_PREPARED,
                recovery_policy=ContinuationRecoveryPolicy.RECONCILE_PREPARED,
                requires_session=True,
                requires_channel=True,
                requires_message=False,
                requires_expected_activation=False,
            ),
            ContinuationOperation.START: _spec(
                ContinuationOperation.START,
                input_schema=_EMPTY_INPUT_SCHEMA,
                activation_effect=ContinuationActivationEffect.START_PREPARED,
                lease_requirement=ContinuationLeaseRequirement.SESSION,
                concurrency=ContinuationConcurrencyRule.SESSION_EXCLUSIVE,
                completion_boundary=ContinuationCompletionBoundary.ACTIVATION_STARTED,
                recovery_policy=ContinuationRecoveryPolicy.RECONCILE_LIVE,
                requires_session=True,
                requires_channel=True,
                requires_message=False,
                requires_expected_activation=True,
            ),
            ContinuationOperation.FOLLOW_UP: _spec(
                ContinuationOperation.FOLLOW_UP,
                input_schema=_OPEN_INPUT_SCHEMA,
                activation_effect=ContinuationActivationEffect.CREATE_NEW,
                lease_requirement=ContinuationLeaseRequirement.SESSION,
                concurrency=ContinuationConcurrencyRule.SESSION_EXCLUSIVE,
                completion_boundary=ContinuationCompletionBoundary.ACTIVATION_TERMINAL,
                recovery_policy=ContinuationRecoveryPolicy.RECONCILE_LIVE,
                requires_session=True,
                requires_channel=True,
                requires_message=True,
                requires_expected_activation=True,
            ),
            ContinuationOperation.REPLY: _spec(
                ContinuationOperation.REPLY,
                input_schema=_OPEN_INPUT_SCHEMA,
                activation_effect=ContinuationActivationEffect.CREATE_NEW,
                lease_requirement=ContinuationLeaseRequirement.SESSION,
                concurrency=ContinuationConcurrencyRule.SESSION_EXCLUSIVE,
                completion_boundary=ContinuationCompletionBoundary.ACTIVATION_TERMINAL,
                recovery_policy=ContinuationRecoveryPolicy.RECONCILE_LIVE,
                requires_session=True,
                requires_channel=True,
                requires_message=True,
                requires_expected_activation=True,
                requires_reply_target=True,
                requires_correlation=True,
            ),
            ContinuationOperation.STEER: _spec(
                ContinuationOperation.STEER,
                input_schema=_OPEN_INPUT_SCHEMA,
                activation_effect=ContinuationActivationEffect.CONTROL_EXISTING,
                lease_requirement=ContinuationLeaseRequirement.SESSION,
                concurrency=ContinuationConcurrencyRule.LIVE_ACTIVATION,
                completion_boundary=ContinuationCompletionBoundary.MESSAGE_COMPLETED,
                recovery_policy=ContinuationRecoveryPolicy.RECONCILE_LIVE,
                requires_session=True,
                requires_channel=True,
                requires_message=True,
                requires_expected_activation=True,
            ),
            ContinuationOperation.PAUSE: _spec(
                ContinuationOperation.PAUSE,
                input_schema=_OPEN_INPUT_SCHEMA,
                activation_effect=ContinuationActivationEffect.CONTROL_EXISTING,
                lease_requirement=ContinuationLeaseRequirement.SESSION,
                concurrency=ContinuationConcurrencyRule.LIVE_ACTIVATION,
                completion_boundary=ContinuationCompletionBoundary.REQUEST_ACCEPTED,
                recovery_policy=ContinuationRecoveryPolicy.RECONCILE_LIVE,
                requires_session=True,
                requires_channel=True,
                requires_message=False,
                requires_expected_activation=True,
            ),
            ContinuationOperation.RESUME: _spec(
                ContinuationOperation.RESUME,
                input_schema=_OPEN_INPUT_SCHEMA,
                activation_effect=ContinuationActivationEffect.CONTROL_EXISTING,
                lease_requirement=ContinuationLeaseRequirement.SESSION,
                concurrency=ContinuationConcurrencyRule.LIVE_ACTIVATION,
                completion_boundary=ContinuationCompletionBoundary.REQUEST_ACCEPTED,
                recovery_policy=ContinuationRecoveryPolicy.RECONCILE_LIVE,
                requires_session=True,
                requires_channel=True,
                requires_message=False,
                requires_expected_activation=True,
            ),
            ContinuationOperation.ACK: _spec(
                ContinuationOperation.ACK,
                input_schema=_OPEN_INPUT_SCHEMA,
                activation_effect=ContinuationActivationEffect.NONE,
                lease_requirement=ContinuationLeaseRequirement.SESSION,
                concurrency=ContinuationConcurrencyRule.SESSION_EXCLUSIVE,
                completion_boundary=ContinuationCompletionBoundary.MESSAGE_COMPLETED,
                recovery_policy=ContinuationRecoveryPolicy.REPLAY_SAFE,
                requires_session=True,
                requires_channel=True,
                requires_message=True,
                requires_expected_activation=False,
                requires_reply_target=True,
            ),
            ContinuationOperation.CANCEL: _spec(
                ContinuationOperation.CANCEL,
                input_schema=_REASON_INPUT_SCHEMA,
                activation_effect=ContinuationActivationEffect.TERMINATE_EXISTING,
                lease_requirement=ContinuationLeaseRequirement.NONE,
                concurrency=ContinuationConcurrencyRule.SESSION_EXCLUSIVE,
                completion_boundary=ContinuationCompletionBoundary.ACTIVATION_TERMINAL,
                recovery_policy=ContinuationRecoveryPolicy.RECONCILE_LIVE,
                requires_session=False,
                requires_channel=False,
                requires_message=False,
                requires_expected_activation=False,
            ),
            ContinuationOperation.CLOSE: _spec(
                ContinuationOperation.CLOSE,
                input_schema=_REASON_INPUT_SCHEMA,
                activation_effect=ContinuationActivationEffect.CLOSE_CHANNEL,
                lease_requirement=ContinuationLeaseRequirement.SESSION,
                concurrency=ContinuationConcurrencyRule.SESSION_EXCLUSIVE,
                completion_boundary=ContinuationCompletionBoundary.CHANNEL_CLOSED,
                recovery_policy=ContinuationRecoveryPolicy.REPLAY_SAFE,
                requires_session=True,
                requires_channel=True,
                requires_message=False,
                requires_expected_activation=False,
            ),
            ContinuationOperation.RECONCILE: _spec(
                ContinuationOperation.RECONCILE,
                input_schema=_EMPTY_INPUT_SCHEMA,
                activation_effect=ContinuationActivationEffect.RECONCILE_EXISTING,
                lease_requirement=ContinuationLeaseRequirement.SESSION,
                concurrency=ContinuationConcurrencyRule.SESSION_EXCLUSIVE,
                completion_boundary=ContinuationCompletionBoundary.RECONCILIATION_RECORDED,
                recovery_policy=ContinuationRecoveryPolicy.REPLAY_SAFE,
                requires_session=True,
                requires_channel=False,
                requires_message=False,
                requires_expected_activation=False,
            ),
        }
    )
)


def continuation_operation_spec(
    operation: ContinuationOperation,
) -> ContinuationOperationSpec:
    try:
        return CONTINUATION_OPERATION_CATALOG[operation]
    except KeyError as exc:
        raise ContractError(
            "continuation.operation_unsupported",
            f"continuation operation {operation.value} is not catalogued",
        ) from exc


def continuation_operation_catalog() -> tuple[ContinuationOperationSpec, ...]:
    return tuple(CONTINUATION_OPERATION_CATALOG[operation] for operation in ContinuationOperation)
