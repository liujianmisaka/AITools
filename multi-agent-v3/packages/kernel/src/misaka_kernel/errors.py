class KernelError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ModuleGraphError(KernelError):
    pass


class ServiceResolutionError(KernelError):
    pass


class LifecycleError(KernelError):
    pass


class HostStateError(KernelError):
    pass


class EventDeclarationError(KernelError):
    pass
