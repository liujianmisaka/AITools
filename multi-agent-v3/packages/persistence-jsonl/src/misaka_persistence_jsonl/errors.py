class DurableStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DurableConflict(DurableStoreError):
    pass


class DurableNotFound(DurableStoreError):
    pass


class DurableCorruption(DurableStoreError):
    pass
