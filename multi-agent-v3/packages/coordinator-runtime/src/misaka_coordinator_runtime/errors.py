class CoordinatorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CoordinatorStateError(CoordinatorError):
    pass


class CoordinatorConflict(CoordinatorError):
    pass


class CoordinatorNotFound(CoordinatorError):
    pass


class QueueCapacityExceeded(CoordinatorError):
    pass
