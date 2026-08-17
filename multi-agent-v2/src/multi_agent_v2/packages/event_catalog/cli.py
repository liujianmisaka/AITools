from __future__ import annotations

import argparse
import sys
from pathlib import Path

from multi_agent_v2.packages.event_catalog.catalog import render_catalog_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate or check the Phase 7 event catalog")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", type=Path)
    arguments = parser.parse_args()
    rendered = render_catalog_json()
    if arguments.check is not None:
        try:
            current = arguments.check.read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"cannot read catalog: {type(exc).__name__}")
        if current != rendered:
            print("event catalog is stale", file=sys.stderr)
            raise SystemExit(1)
        return
    if arguments.output is not None:
        arguments.output.write_text(rendered, encoding="utf-8", newline="\n")
        return
    sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
