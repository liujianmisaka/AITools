from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from pydantic import SecretStr

from multi_agent_v2.packages.credentials.models import (
    CredentialInfo,
    CredentialReadOnlyError,
    CredentialRef,
    CredentialStoreCorruptError,
    ResolvedCredential,
)


class LocalCredentialProvider:
    """Resolves local credentials from environment first, then an atomic JSON file."""

    def __init__(
        self,
        path: Path,
        *,
        environment: Mapping[str, str] | None = None,
        environment_prefix: str = "MULTI_AGENT_V2_CREDENTIAL_",
    ) -> None:
        self._path = path.expanduser().resolve()
        self._environment = environment if environment is not None else os.environ
        self._environment_prefix = environment_prefix
        self._write_lock = asyncio.Lock()

    async def resolve(self, reference: CredentialRef) -> ResolvedCredential | None:
        environment_value = self._environment_value(reference)
        if environment_value is not None:
            return ResolvedCredential(
                reference=reference,
                value=SecretStr(environment_value),
                source="environment",
            )
        values = await asyncio.to_thread(self._read_values)
        file_value = _non_blank(values.get(reference.name))
        if file_value is None:
            return None
        return ResolvedCredential(
            reference=reference,
            value=SecretStr(file_value),
            source="file",
        )

    async def info(self, reference: CredentialRef) -> CredentialInfo:
        resolved = await self.resolve(reference)
        if resolved is None:
            return CredentialInfo(
                reference=reference,
                configured=False,
                source="absent",
                writable=True,
            )
        return CredentialInfo(
            reference=reference,
            configured=True,
            source=resolved.source,
            writable=resolved.source == "file",
        )

    async def set(self, reference: CredentialRef, value: str | None) -> CredentialInfo:
        if self._environment_value(reference) is not None:
            raise CredentialReadOnlyError(
                f"credential '{reference.name}' is supplied by the environment and is read-only"
            )
        normalized = _non_blank(value)
        async with self._write_lock:
            await asyncio.to_thread(self._write_value, reference.name, normalized)
        return await self.info(reference)

    def environment_name(self, reference: CredentialRef) -> str:
        substitutions = {".": "__DOT__", "_": "__UNDERSCORE__", "-": "__DASH__"}
        suffix = "".join(
            character.upper() if character.isalnum() else substitutions[character]
            for character in reference.name
        )
        return f"{self._environment_prefix}{suffix}"

    def _environment_value(self, reference: CredentialRef) -> str | None:
        return _non_blank(self._environment.get(self.environment_name(reference)))

    def _read_values(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            raw: object = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreCorruptError("credential store cannot be read") from exc
        if not isinstance(raw, dict):
            raise CredentialStoreCorruptError("credential store has an unsupported format")
        document = cast(dict[object, object], raw)
        if document.get("version") != 1:
            raise CredentialStoreCorruptError("credential store has an unsupported format")
        credentials = document.get("credentials")
        if not isinstance(credentials, dict):
            raise CredentialStoreCorruptError("credential store credentials must be an object")
        credential_values = cast(dict[object, object], credentials)
        values: dict[str, str] = {}
        for key, value in credential_values.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise CredentialStoreCorruptError("credential store values must be strings")
            try:
                reference = CredentialRef(name=key)
            except ValueError as exc:
                raise CredentialStoreCorruptError(
                    "credential store contains an invalid reference"
                ) from exc
            candidate = _non_blank(value)
            if candidate is not None:
                values[reference.name] = candidate
        return values

    def _write_value(self, name: str, value: str | None) -> None:
        values = self._read_values()
        if value is None:
            values.pop(name, None)
        else:
            values[name] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{secrets.token_hex(8)}.tmp")
        document = {
            "version": 1,
            "credentials": dict(sorted(values.items())),
        }
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    document,
                    stream,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, self._path)
            _fsync_directory(self._path.parent)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _non_blank(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate or None


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
