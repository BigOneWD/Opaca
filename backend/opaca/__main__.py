"""Opaca CLI. Read-only preflight is the only command in this phase."""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        sys.stdout.write(
            "usage: python -m opaca preflight [--db PATH] [--overwrite-db]\n"
            "\n"
            "preflight  read-only PAPER checks; never submits or cancels\n"
        )
        return 0
    command, rest = args[0], args[1:]
    if command != "preflight":
        sys.stderr.write(f"unknown command {command!r}; try preflight\n")
        return 2
    from opaca.preflight import main as preflight_main

    return preflight_main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
