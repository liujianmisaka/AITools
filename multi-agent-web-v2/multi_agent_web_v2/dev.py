from __future__ import annotations

from pathlib import Path

from watchfiles import run_process

from multi_agent_web_v2.main import run as run_servers


def run() -> None:
    package_root = Path(__file__).resolve().parent
    raise SystemExit(
        run_process(
            package_root,
            target=run_servers,
            target_type="function",
        )
    )
