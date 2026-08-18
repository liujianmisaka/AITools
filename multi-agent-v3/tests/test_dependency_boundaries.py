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
