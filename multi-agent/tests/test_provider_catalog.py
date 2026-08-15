from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

from multi_agent.providers.codex import CodexProvider
from multi_agent.providers.runtime import CodexEnvironmentKind, CodexRuntimeLocator


def _model(model_id: str = "gpt-5.6-sol") -> dict:
    return {
        "id": model_id,
        "model": model_id,
        "displayName": model_id,
        "hidden": False,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "fast"},
            {"reasoningEffort": "medium", "description": "balanced"},
            {"reasoningEffort": "high", "description": "deep"},
        ],
    }


class _CodexConfig:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class _CatalogClient:
    def __init__(self, response: dict, clients: list["_CatalogClient"]) -> None:
        self.response = response
        self.entered = False
        self.closed = False
        self.include_hidden = None
        clients.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_exc_info):
        self.closed = True

    async def models(self, *, include_hidden: bool = False):
        self.include_hidden = include_hidden
        return self.response


def _sdk(response: dict, clients: list[_CatalogClient]):
    return types.SimpleNamespace(
        CodexConfig=_CodexConfig,
        AsyncCodex=lambda _config: _CatalogClient(response, clients),
    )


class CodexModelCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_native_openai_models_without_catalog_file(self) -> None:
        clients: list[_CatalogClient] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            (home / "config.toml").write_text(
                'model = "gpt-5.6-sol"',
                encoding="utf-8",
            )
            provider = CodexProvider(
                codex_home=str(home),
                sdk_module=_sdk({"data": [_model()]}, clients),
            )

            models = await provider.models()
            metadata = await provider.metadata()

        self.assertEqual([model.id for model in models], ["gpt-5.6-sol"])
        self.assertEqual(models[0].model_type, "openai")
        self.assertEqual(models[0].efforts, ("low", "medium", "high"))
        self.assertEqual(metadata["environment_kind"], "openai_native")
        self.assertEqual(metadata["model_catalog"], "codex_app_server")
        self.assertEqual(len(clients), 1)
        self.assertTrue(clients[0].entered)
        self.assertTrue(clients[0].closed)
        self.assertFalse(clients[0].include_hidden)

    async def test_discovers_ccswitch_projected_catalog(self) -> None:
        clients: list[_CatalogClient] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            (home / "config.toml").write_text(
                "\n".join(
                    [
                        'model_provider = "custom"',
                        'model_catalog_json = "cc-switch-model-catalog.json"',
                        "[model_providers.custom]",
                        'name = "CC Switch"',
                        'base_url = "http://127.0.0.1:15721/v1"',
                        'wire_api = "responses"',
                    ]
                ),
                encoding="utf-8",
            )
            provider = CodexProvider(
                codex_home=str(home),
                sdk_module=_sdk(
                    {"data": [_model("deepseek/deepseek-v4-flash")]},
                    clients,
                ),
            )

            models = await provider.models()
            metadata = await provider.metadata()

        self.assertEqual(models[0].model_type, "deepseek")
        self.assertEqual(metadata["model_provider"], "custom")
        self.assertEqual(metadata["environment_kind"], "ccswitch")
        self.assertTrue(str(metadata["catalog_path"]).endswith(
            "cc-switch-model-catalog.json"
        ))

    async def test_discovers_opencodex_projected_catalog(self) -> None:
        clients: list[_CatalogClient] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            (home / "config.toml").write_text(
                "\n".join(
                    [
                        'model_catalog_json = "opencodex-catalog.json"',
                        'openai_base_url = "http://127.0.0.1:10100/v1"',
                    ]
                ),
                encoding="utf-8",
            )
            provider = CodexProvider(
                codex_home=str(home),
                sdk_module=_sdk(
                    {"data": [_model("sensenova/deepseek-v4-flash")]},
                    clients,
                ),
            )

            models = await provider.models()
            metadata = await provider.metadata()

        self.assertEqual(models[0].model_type, "sensenova")
        self.assertEqual(metadata["model_provider"], "openai")
        self.assertEqual(metadata["environment_kind"], "opencodex")

    async def test_refreshes_after_effective_config_changes(self) -> None:
        clients: list[_CatalogClient] = []
        responses = [
            {"data": [_model("first/model")]},
            {"data": [_model("second/model-with-different-size")]},
        ]

        def client_factory(_config):
            return _CatalogClient(responses.pop(0), clients)

        sdk = types.SimpleNamespace(
            CodexConfig=_CodexConfig,
            AsyncCodex=client_factory,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            config = home / "config.toml"
            config.write_text('model = "first/model"', encoding="utf-8")
            provider = CodexProvider(
                codex_home=str(home),
                sdk_module=sdk,
                catalog_ttl_seconds=300,
            )

            first = await provider.models()
            config.write_text(
                'model = "second/model-with-different-size"',
                encoding="utf-8",
            )
            second = await provider.models()

        self.assertEqual(first[0].id, "first/model")
        self.assertEqual(second[0].id, "second/model-with-different-size")
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(client.closed for client in clients))

    async def test_rejects_duplicate_models_from_app_server(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            provider = CodexProvider(
                codex_home=str(home),
                sdk_module=_sdk(
                    {"data": [_model("same/model"), _model("same/model")]},
                    [],
                ),
            )

            with self.assertRaisesRegex(Exception, "duplicate model ids"):
                await provider.models()

    def test_uses_ccswitch_custom_codex_directory_when_no_explicit_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            user_home = Path(temporary_directory)
            codex_home = user_home / "ccswitch-codex"
            codex_home.mkdir()
            settings_home = user_home / ".cc-switch"
            settings_home.mkdir()
            (settings_home / "settings.json").write_text(
                json.dumps({"codexConfigDir": str(codex_home)}),
                encoding="utf-8",
            )

            runtime = CodexRuntimeLocator(
                environ={},
                user_home=user_home,
            ).resolve()

        self.assertEqual(runtime.codex_home, codex_home.resolve())
        self.assertEqual(runtime.config_source, "ccswitch_settings")
        self.assertEqual(runtime.environment_kind, CodexEnvironmentKind.ccswitch)

    def test_rejects_custom_provider_without_definition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            (home / "config.toml").write_text(
                'model_provider = "custom"',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "model_providers.custom"):
                CodexRuntimeLocator(codex_home=home).resolve()


if __name__ == "__main__":
    unittest.main()
