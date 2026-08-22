from pathlib import Path
from runpy import run_path

from misaka_session_capability import MemorySessionStore

_ENTRY = run_path(str(Path(__file__).parents[1] / "examples" / "control_plane_codex.py"))
_create_provider = _ENTRY["_create_provider"]


def test_codex_provider_is_bound_to_profile_session_store(tmp_path: Path) -> None:
    provider = _create_provider(
        provider_id="codex",
        codex_home=tmp_path / "codex-home",
        network_deny_enforced=False,
    )

    assert isinstance(provider.session_store, MemorySessionStore)
