from pathlib import Path
from runpy import run_path

import pytest
from misaka_session_capability import MemorySessionStore

_ENTRY = run_path(str(Path(__file__).parents[1] / "examples" / "control_plane_codex.py"))
_create_provider = _ENTRY["_create_provider"]
_workspace_entries = _ENTRY["_workspace_entries"]


def test_workspace_entries_use_stable_defaults(tmp_path: Path) -> None:
    roots = (tmp_path / "one", tmp_path / "two")

    assert _workspace_entries(roots, None) == {
        "workspace-1": roots[0],
        "workspace-2": roots[1],
    }


def test_workspace_entries_accept_explicit_ids(tmp_path: Path) -> None:
    roots = (tmp_path / "one", tmp_path / "two")

    assert _workspace_entries(roots, ("source", "target")) == {
        "source": roots[0],
        "target": roots[1],
    }


def test_codex_provider_is_bound_to_profile_session_store(tmp_path: Path) -> None:
    provider = _create_provider(
        provider_id="codex",
        codex_home=tmp_path / "codex-home",
        workspace_roots=(tmp_path,),
        network_deny_enforced=False,
    )

    assert isinstance(provider.session_store, MemorySessionStore)


@pytest.mark.parametrize(
    "workspace_ids",
    [("only-one",), ("duplicate", "duplicate"), ("", "valid")],
)
def test_workspace_entries_reject_invalid_ids(
    tmp_path: Path, workspace_ids: tuple[str, ...]
) -> None:
    roots = (tmp_path / "one", tmp_path / "two")

    with pytest.raises(ValueError):
        _workspace_entries(roots, workspace_ids)
