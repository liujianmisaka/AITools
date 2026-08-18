class A2AError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class TaskIdempotencyConflict(A2AError):
    pass


class TaskNotFound(A2AError):
    pass


class TaskCapabilityRejected(A2AError):
    pass


class TaskStateError(A2AError):
    pass


class A2AServerStateError(A2AError):
    pass
