from __future__ import annotations

import tempfile
from pathlib import Path

from aitools_service_manager.config import RuntimeConfiguration


class ProviderRuntimeAccessError(RuntimeError):
    def __init__(self, provider_id: str, path: Path, error: OSError) -> None:
        super().__init__(
            f"Codex provider {provider_id} cannot write its configured home {path}: {error}. "
            "Start the AITools Management API from a host that can write this directory, "
            "or select a writable authenticated Codex home."
        )
        self.provider_id = provider_id
        self.path = path


def validate_provider_runtime_access(configuration: RuntimeConfiguration) -> None:
    """Fail before Control Plane startup when a Provider cannot use its runtime home."""

    for provider in configuration.providers:
        if provider.kind != "codex":
            continue
        codex_home = provider.codex_home
        if codex_home is None:
            raise AssertionError("validated Codex provider must define codex_home")
        try:
            _probe_writable_directory(codex_home)
        except OSError as exc:
            raise ProviderRuntimeAccessError(provider.provider_id, codex_home, exc) from exc


def _probe_writable_directory(path: Path) -> None:
    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".aitools-codex-write-probe-",
            suffix=".tmp",
            dir=path,
            delete=False,
        ) as probe:
            probe.write(b"AITools Codex runtime preflight\n")
            probe.flush()
            probe_path = Path(probe.name)
    finally:
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)
