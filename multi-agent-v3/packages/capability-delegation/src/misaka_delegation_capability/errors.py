class DelegationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DelegationConflict(DelegationError):
    pass


class DelegationNotFound(DelegationError):
    pass


class DelegationStateError(DelegationError):
    pass


class DelegationUnauthorized(DelegationError):
    pass


class DelegationCapabilityRejected(DelegationError):
    pass
