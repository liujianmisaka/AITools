from __future__ import annotations


class ApprovalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ApprovalConflict(ApprovalError):
    pass


class ApprovalNotFound(ApprovalError):
    pass
