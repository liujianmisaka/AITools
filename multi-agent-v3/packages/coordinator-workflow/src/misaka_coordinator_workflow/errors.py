class WorkflowCoordinatorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class WorkflowDefinitionError(WorkflowCoordinatorError):
    pass


class WorkflowStateError(WorkflowCoordinatorError):
    pass
