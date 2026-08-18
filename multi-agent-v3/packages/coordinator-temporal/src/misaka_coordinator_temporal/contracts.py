from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from misaka_invocation_contracts import (
    ArtifactRef,
    CapabilityFeature,
    CompletionBoundary,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    SessionRef,
)
from misaka_kernel_contracts import JsonObject


@dataclass(frozen=True, slots=True)
class TemporalInvocationInput:
    """Temporal-safe primitive DTO; core JsonObject aliases stay outside the payload boundary."""

    invocation_id: str
    capability_id: str
    operation: str
    input: Any
    idempotency_key: str
    completion_boundary: str
    parent_invocation_id: str | None = None
    session_provider: str | None = None
    session_native_id: str | None = None
    required_features: tuple[str, ...] = ()
    output_schema: Any = None
    policy_context: Any = None
    attempt: int = 1
    model: str | None = None
    effort: str | None = None
    provider_id: str | None = None
    start_to_close_timeout_seconds: int = 300
    heartbeat_timeout_seconds: int = 30
    heartbeat_interval_seconds: float = 5.0
    maximum_attempts: int = 1

    @classmethod
    def from_request(
        cls,
        request: InvocationRequest,
        *,
        provider_id: str | None = None,
        start_to_close_timeout_seconds: int = 300,
        heartbeat_timeout_seconds: int = 30,
        heartbeat_interval_seconds: float = 5.0,
        maximum_attempts: int = 1,
    ) -> TemporalInvocationInput:
        session_provider = request.session_ref.provider if request.session_ref else None
        session_native_id = request.session_ref.native_id if request.session_ref else None
        return cls(
            invocation_id=request.invocation_id,
            capability_id=request.capability_id,
            operation=request.operation,
            input=request.input,
            idempotency_key=request.idempotency_key,
            completion_boundary=request.completion_boundary.value,
            parent_invocation_id=request.parent_invocation_id,
            session_provider=session_provider,
            session_native_id=session_native_id,
            required_features=tuple(feature.value for feature in request.required_features),
            output_schema=request.output_schema,
            policy_context=request.policy_context,
            attempt=request.attempt,
            model=request.model,
            effort=request.effort,
            provider_id=provider_id,
            start_to_close_timeout_seconds=start_to_close_timeout_seconds,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            maximum_attempts=maximum_attempts,
        )

    def to_request(self) -> InvocationRequest:
        session_ref = None
        if self.session_provider is not None or self.session_native_id is not None:
            if self.session_provider is None or self.session_native_id is None:
                raise ValueError("Temporal session reference must include provider and native id")
            session_ref = SessionRef(self.session_provider, self.session_native_id)
        input_value = self.input
        output_schema = self.output_schema
        policy_context = self.policy_context
        if not isinstance(input_value, dict):
            raise ValueError("Temporal invocation input must be an object")
        if output_schema is not None and not isinstance(output_schema, dict):
            raise ValueError("Temporal output_schema must be an object")
        if policy_context is not None and not isinstance(policy_context, dict):
            raise ValueError("Temporal policy_context must be an object")
        input_object = cast(JsonObject, input_value)
        output_object = cast(JsonObject, output_schema) if output_schema is not None else None
        policy_object = cast(JsonObject, policy_context) if policy_context is not None else {}
        return InvocationRequest(
            invocation_id=self.invocation_id,
            capability_id=self.capability_id,
            operation=self.operation,
            input=input_object,
            idempotency_key=self.idempotency_key,
            completion_boundary=CompletionBoundary(self.completion_boundary),
            parent_invocation_id=self.parent_invocation_id,
            session_ref=session_ref,
            required_features=frozenset(
                CapabilityFeature(feature) for feature in self.required_features
            ),
            output_schema=output_object,
            policy_context=policy_object,
            attempt=self.attempt,
            model=self.model,
            effort=self.effort,
        )

    def __post_init__(self) -> None:
        if self.provider_id is not None and not self.provider_id.strip():
            raise ValueError("provider_id must not be empty when provided")
        if self.start_to_close_timeout_seconds < 1 or self.heartbeat_timeout_seconds < 1:
            raise ValueError("Temporal timeout values must be positive")
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        if self.heartbeat_interval_seconds >= self.heartbeat_timeout_seconds:
            raise ValueError("heartbeat interval must be shorter than heartbeat timeout")
        if self.maximum_attempts < 1:
            raise ValueError("maximum_attempts must be at least one")


@dataclass(frozen=True, slots=True)
class TemporalResultPayload:
    invocation_id: str
    status: str
    output: Any = None
    error_code: str | None = None
    error_message: str | None = None
    artifacts: Any = None

    @classmethod
    def from_result(cls, result: InvocationResult) -> TemporalResultPayload:
        return cls(
            invocation_id=result.invocation_id,
            status=result.status.value,
            output=result.output,
            error_code=result.error_code,
            error_message=result.error_message,
            artifacts=[
                {
                    "artifact_id": artifact.artifact_id,
                    "media_type": artifact.media_type,
                    "size_bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                    "location": artifact.location,
                    "metadata": artifact.metadata,
                }
                for artifact in result.artifacts
            ],
        )

    def to_result(self) -> InvocationResult:
        artifacts: list[ArtifactRef] = []
        if self.artifacts is not None:
            if not isinstance(self.artifacts, list):
                raise ValueError("Temporal artifacts must be a list")
            artifact_items = cast(list[dict[str, Any]], self.artifacts)
            for item in artifact_items:
                artifacts.append(
                    ArtifactRef(
                        artifact_id=str(item["artifact_id"]),
                        media_type=str(item["media_type"]),
                        size_bytes=int(item["size_bytes"]),
                        sha256=str(item["sha256"]),
                        location=str(item["location"]),
                        metadata=cast(JsonObject, item.get("metadata", {})),
                    )
                )
        return InvocationResult(
            invocation_id=self.invocation_id,
            status=InvocationStatus(self.status),
            output=self.output,
            error_code=self.error_code,
            error_message=self.error_message,
            artifacts=tuple(artifacts),
        )
