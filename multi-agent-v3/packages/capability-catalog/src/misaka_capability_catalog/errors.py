class CapabilityCatalogError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ProviderRegistrationConflict(CapabilityCatalogError):
    pass


class ProviderRegistrationNotFound(CapabilityCatalogError):
    pass


class CapabilityCatalogAmbiguous(CapabilityCatalogError):
    pass
