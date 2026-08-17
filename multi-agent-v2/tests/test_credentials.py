from __future__ import annotations

import json
from pathlib import Path

import pytest

from multi_agent_v2.packages.credentials import (
    CredentialReadOnlyError,
    CredentialRef,
    CredentialStoreCorruptError,
    LocalCredentialProvider,
)


async def test_environment_shadows_file_without_exposing_the_value(tmp_path: Path) -> None:
    store = tmp_path / "credentials.json"
    store.write_text(
        json.dumps({"version": 1, "credentials": {"webhook.hmac": "file-secret"}}),
        encoding="utf-8",
    )
    provider = LocalCredentialProvider(
        store,
        environment={"MULTI_AGENT_V2_CREDENTIAL_WEBHOOK__DOT__HMAC": "environment-secret"},
    )
    reference = CredentialRef(name="webhook.hmac")

    resolved = await provider.resolve(reference)
    info = await provider.info(reference)

    assert resolved is not None
    assert resolved.value.get_secret_value() == "environment-secret"
    assert "environment-secret" not in repr(resolved)
    assert info.model_dump(mode="json") == {
        "reference": {"name": "webhook.hmac"},
        "configured": True,
        "source": "environment",
        "writable": False,
    }
    with pytest.raises(CredentialReadOnlyError):
        await provider.set(reference, "replacement")


async def test_file_credentials_rotate_per_operation_and_blank_removes(tmp_path: Path) -> None:
    provider = LocalCredentialProvider(tmp_path / "credentials.json", environment={})
    reference = CredentialRef(name="webhook.hmac")

    assert await provider.resolve(reference) is None
    created = await provider.set(reference, "first")
    first = await provider.resolve(reference)
    await provider.set(reference, "second")
    second = await provider.resolve(reference)
    removed = await provider.set(reference, "   ")

    assert created.source == "file"
    assert first is not None and first.value.get_secret_value() == "first"
    assert second is not None and second.value.get_secret_value() == "second"
    assert removed.configured is False
    assert await provider.resolve(reference) is None


async def test_corrupt_store_fails_without_returning_raw_contents(tmp_path: Path) -> None:
    store = tmp_path / "credentials.json"
    store.write_text('{"credentials":{"webhook.hmac":"do-not-leak"}', encoding="utf-8")
    provider = LocalCredentialProvider(store, environment={})

    with pytest.raises(CredentialStoreCorruptError) as captured:
        await provider.resolve(CredentialRef(name="webhook.hmac"))

    assert "do-not-leak" not in str(captured.value)


def test_environment_names_do_not_collide_between_supported_reference_characters(
    tmp_path: Path,
) -> None:
    provider = LocalCredentialProvider(tmp_path / "credentials.json", environment={})

    names = {
        provider.environment_name(CredentialRef(name=reference))
        for reference in ("a.b", "a_b", "a-b")
    }

    assert len(names) == 3
