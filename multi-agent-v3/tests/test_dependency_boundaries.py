import ast
from pathlib import Path
from subprocess import run
from sys import executable

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_v3_package_import_boundaries() -> None:
    result = run(
        [
            executable,
            str(PROJECT_ROOT / "tools" / "check_import_boundaries.py"),
            str(PROJECT_ROOT),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_a2a_only_profile_does_not_import_optional_platform_stacks() -> None:
    package_roots = (
        PROJECT_ROOT / "packages" / "capability-a2a",
        PROJECT_ROOT / "packages" / "a2a-runtime",
        PROJECT_ROOT / "packages" / "transport-a2a-http",
        PROJECT_ROOT / "packages" / "profile-a2a-node",
    )
    forbidden_roots = {
        "alembic",
        "asyncpg",
        "fastapi",
        "misaka_codex_provider",
        "sqlalchemy",
        "temporalio",
    }
    imported: set[str] = set()
    for package_root in package_roots:
        for source in package_root.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".", 1)[0])

    assert imported.isdisjoint(forbidden_roots), sorted(imported & forbidden_roots)
