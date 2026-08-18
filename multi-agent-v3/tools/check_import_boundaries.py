from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True, slots=True)
class PackageRule:
    import_name: str
    source_path: Path
    allowed_internal: frozenset[str]
    stdlib_only: bool


@dataclass(frozen=True, slots=True)
class ImportViolation:
    path: Path
    line: int
    imported_name: str
    message: str


def load_rules(project_root: Path) -> tuple[PackageRule, ...]:
    rules_path = project_root / "dependency-rules.toml"
    with rules_path.open("rb") as stream:
        document = cast(dict[str, object], tomllib.load(stream))

    raw_packages = document.get("packages")
    if not isinstance(raw_packages, dict) or not raw_packages:
        raise ValueError("dependency-rules.toml must declare at least one package")
    packages = cast(dict[str, object], raw_packages)

    rules: list[PackageRule] = []
    for import_name, raw_value in packages.items():
        if not isinstance(raw_value, dict):
            raise ValueError("package dependency rules must be TOML tables")
        value = cast(dict[str, object], raw_value)
        relative_path = value.get("path")
        raw_allowed_internal = value.get("allowed_internal", [])
        stdlib_only = value.get("stdlib_only", False)
        if not isinstance(relative_path, str):
            raise ValueError(f"package {import_name} is missing a path")
        if not isinstance(raw_allowed_internal, list):
            raise ValueError(f"package {import_name} has invalid allowed_internal entries")
        allowed_values = cast(list[object], raw_allowed_internal)
        if not all(isinstance(item, str) for item in allowed_values):
            raise ValueError(f"package {import_name} has invalid allowed_internal entries")
        allowed_internal = tuple(cast(str, item) for item in allowed_values)
        if not isinstance(stdlib_only, bool):
            raise ValueError(f"package {import_name} has an invalid stdlib_only value")
        source_path = project_root / relative_path
        if not source_path.is_dir():
            raise ValueError(f"package {import_name} source path does not exist: {source_path}")
        rules.append(
            PackageRule(
                import_name=import_name,
                source_path=source_path,
                allowed_internal=frozenset(allowed_internal),
                stdlib_only=stdlib_only,
            )
        )
    return tuple(rules)


def check_boundaries(project_root: Path) -> tuple[ImportViolation, ...]:
    rules = load_rules(project_root)
    configured_names = frozenset(rule.import_name for rule in rules)
    violations: list[ImportViolation] = []
    for rule in rules:
        for path in sorted(rule.source_path.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for imported_name, line in _imports(tree):
                root_name = imported_name.partition(".")[0]
                if root_name == rule.import_name:
                    continue
                if root_name in configured_names:
                    if root_name not in rule.allowed_internal:
                        violations.append(
                            ImportViolation(
                                path,
                                line,
                                imported_name,
                                f"{rule.import_name} cannot depend on {root_name}",
                            )
                        )
                    continue
                if rule.stdlib_only and root_name not in sys.stdlib_module_names:
                    violations.append(
                        ImportViolation(
                            path,
                            line,
                            imported_name,
                            f"{rule.import_name} only permits standard-library imports",
                        )
                    )

    discovered = {
        path.name for path in (project_root / "packages").glob("*/src/misaka_*") if path.is_dir()
    }
    for missing_name in sorted(discovered - configured_names):
        violations.append(
            ImportViolation(
                project_root / "dependency-rules.toml",
                1,
                missing_name,
                "source package is missing a dependency rule",
            )
        )
    return tuple(violations)


def _imports(tree: ast.AST) -> tuple[tuple[str, int], ...]:
    values: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            values.append((node.module, node.lineno))
    return tuple(values)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check V3 package import boundaries")
    parser.add_argument(
        "project_root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    project_root = parser.parse_args().project_root.resolve()
    try:
        violations = check_boundaries(project_root)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"dependency boundary configuration error: {exc}", file=sys.stderr)
        return 2
    if not violations:
        print("V3 dependency boundaries are valid")
        return 0
    for violation in violations:
        relative_path = violation.path.relative_to(project_root)
        print(
            f"{relative_path}:{violation.line}: {violation.message}: {violation.imported_name}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
