from __future__ import annotations


class OrchestrationError(RuntimeError):
    """Base class for stable application errors."""

    code = "orchestration_error"


class WorkflowInstanceNotFoundError(OrchestrationError):
    code = "workflow_instance_not_found"


class WorkflowTemplateNotFoundError(OrchestrationError):
    code = "workflow_template_not_found"


class WorkflowTemplateVersionConflictError(OrchestrationError):
    code = "workflow_template_version_conflict"


class WorkflowTemplateCursorError(OrchestrationError):
    code = "invalid_workflow_template_cursor"


class WorkflowInstanceCursorError(OrchestrationError):
    code = "invalid_workflow_instance_cursor"


class OrchestrationModelNotFoundError(OrchestrationError):
    code = "orchestration_model_not_found"


class EventSourceNotFoundError(OrchestrationError):
    code = "event_source_not_found"


class TriggerBindingNotFoundError(OrchestrationError):
    code = "trigger_binding_not_found"


class TriggerBindingConflictError(OrchestrationError):
    code = "trigger_binding_conflict"


class TriggerEventNotFoundError(OrchestrationError):
    code = "trigger_event_not_found"


class TriggerEventProcessingError(OrchestrationError):
    code = "trigger_event_processing_error"


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
