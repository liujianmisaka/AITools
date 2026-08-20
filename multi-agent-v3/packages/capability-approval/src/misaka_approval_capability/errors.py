from __future__ import annotations


class DecisionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DecisionConflict(DecisionError):
    pass


class DecisionNotFound(DecisionError):
    pass


class DecisionRequired(DecisionError):
    pass


class DecisionDenied(DecisionError):
    pass
