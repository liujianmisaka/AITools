from __future__ import annotations


class OrchestrationError(RuntimeError):
    """Base class for stable application errors."""

    code = "orchestration_error"


class RunNotFoundError(OrchestrationError):
    code = "run_not_found"


class ApprovalNotFoundError(OrchestrationError):
    code = "approval_not_found"


class ApprovalStateError(OrchestrationError):
    code = "approval_state_error"


class WorkspaceNotAllowedError(OrchestrationError):
    code = "workspace_not_allowed"


class ProviderNotFoundError(OrchestrationError):
    code = "provider_not_found"


class ProviderUnavailableError(OrchestrationError):
    code = "provider_unavailable"


class ProviderCapabilityError(OrchestrationError):
    code = "provider_capability_error"


class InvalidOutputSchemaError(OrchestrationError):
    code = "invalid_output_schema"


class ProviderExecutionError(OrchestrationError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_execution_error",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class CoordinatorUnavailableError(OrchestrationError):
    code = "coordinator_unavailable"


class CoordinatorOutputError(OrchestrationError):
    code = "coordinator_output_error"


class CoordinatorContractError(OrchestrationError):
    code = "coordinator_contract_error"
