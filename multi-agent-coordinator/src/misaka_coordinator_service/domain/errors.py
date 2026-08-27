class CoordinatorDomainError(ValueError):
    """Base error for invalid coordinator domain operations."""


class InvalidTransitionError(CoordinatorDomainError):
    """Raised when an aggregate cannot perform the requested transition."""
