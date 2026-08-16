from __future__ import annotations

from pathlib import Path

import pytest

from multi_agent_v2.packages.agent_runtime.codex_catalog import catalog_from_app_server
from multi_agent_v2.packages.agent_runtime.codex_locator import (
    CodexEnvironmentKind,
    CodexRuntimeDescriptor,
)


def _runtime() -> CodexRuntimeDescriptor:
    return CodexRuntimeDescriptor(
        runtime_id="codex:opencodex:sensenova:test",
        codex_bin="codex.exe",
        codex_home=Path("D:/codex"),
        config_path=Path("D:/codex/config.toml"),
        config_source="explicit",
        provider_id="sensenova",
        environment_kind=CodexEnvironmentKind.opencodex,
        catalog_path=Path("D:/codex/opencodex-models.json"),
        signature=("test",),
    )


def test_catalog_reads_explicit_models_and_efforts() -> None:
    catalog = catalog_from_app_server(
        _runtime(),
        {
            "data": [
                {
                    "id": "sensenova/deepseek-v4-flash",
                    "displayName": "DeepSeek V4 Flash",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "high"},
                        {"reasoningEffort": "ultra"},
                    ],
                    "defaultReasoningEffort": "high",
                },
                {"id": "hidden", "hidden": True},
            ]
        },
    )

    assert [model.id for model in catalog.models] == ["sensenova/deepseek-v4-flash"]
    assert catalog.models[0].efforts == ("high", "ultra")
    assert catalog.models[0].recommended_effort == "high"


def test_catalog_rejects_partial_pagination() -> None:
    with pytest.raises(ValueError, match="nextCursor"):
        catalog_from_app_server(
            _runtime(),
            {"data": [], "nextCursor": "next-page"},
        )
