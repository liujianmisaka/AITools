from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_REFERENCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class CredentialRef(BaseModel):
    """A stable local reference that never contains the credential value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        candidate = value.strip().lower()
        if not _REFERENCE_PATTERN.fullmatch(candidate):
            raise ValueError(
                "credential reference must use lowercase letters, digits, '.', '_' or '-'"
            )
        return candidate


class ResolvedCredential(BaseModel):
    """A per-operation secret resolution; callers must not persist this object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: CredentialRef
    value: SecretStr = Field(repr=False)
    source: Literal["environment", "file"]


class CredentialInfo(BaseModel):
    """Safe metadata suitable for logs and UI responses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: CredentialRef
    configured: bool
    source: Literal["environment", "file", "absent"]
    writable: bool


class CredentialError(RuntimeError):
    code = "credential.error"


class CredentialReadOnlyError(CredentialError):
    code = "credential.read_only"


class CredentialStoreCorruptError(CredentialError):
    code = "credential.store_corrupt"
