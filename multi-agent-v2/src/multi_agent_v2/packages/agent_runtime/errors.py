from __future__ import annotations


class AgentRuntimeError(RuntimeError):
    """Stable local Agent runtime failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "agent.runtime_error",
        retryable: bool = False,
        reconciliation_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.reconciliation_required = reconciliation_required


class AgentStreamContractError(AgentRuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="agent.stream_contract_violated")
