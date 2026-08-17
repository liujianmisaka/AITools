"""Local platform credential references and per-operation resolution."""

from multi_agent_v2.packages.credentials.local import LocalCredentialProvider
from multi_agent_v2.packages.credentials.models import (
    CredentialError,
    CredentialInfo,
    CredentialReadOnlyError,
    CredentialRef,
    CredentialStoreCorruptError,
    ResolvedCredential,
)
from multi_agent_v2.packages.credentials.protocol import CredentialProvider

__all__ = [
    "CredentialError",
    "CredentialInfo",
    "CredentialProvider",
    "CredentialReadOnlyError",
    "CredentialRef",
    "CredentialStoreCorruptError",
    "LocalCredentialProvider",
    "ResolvedCredential",
]
