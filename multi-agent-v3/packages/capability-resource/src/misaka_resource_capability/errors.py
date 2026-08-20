from __future__ import annotations


class ResourceCapabilityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ResourceBusy(ResourceCapabilityError):
    pass


class ResourceFenced(ResourceCapabilityError):
    pass


class LeaseExpired(ResourceCapabilityError):
    pass


class SandboxUnavailable(ResourceCapabilityError):
    pass


class CredentialNotFound(ResourceCapabilityError):
    pass


class SettingsNotFound(ResourceCapabilityError):
    pass


class SettingsConflict(ResourceCapabilityError):
    pass
