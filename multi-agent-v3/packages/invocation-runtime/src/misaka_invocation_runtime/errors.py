class InvocationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class IdempotencyConflict(InvocationError):
    pass


class CapabilityUnavailable(InvocationError):
    pass


class ProviderContractError(InvocationError):
    pass


class ProviderExecutionError(InvocationError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        reconciliation_required: bool = False,
    ) -> None:
        self.reconciliation_required = reconciliation_required
        super().__init__(code, message)
