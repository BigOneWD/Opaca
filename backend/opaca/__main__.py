"""Opaca command-line entry point."""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        sys.stdout.write(
            "usage: python -m opaca <command> [options]\n"
            "\n"
            "preflight    read-only PAPER checks; never submits or cancels\n"
            "intake-demo  read-only AI obligation intake; no broker mutation capability\n"
            "wheel-probe  dry read-only Wheel readiness and MCP-surface check\n"
            "wheel-plan   dry deterministic Wheel intent/selector/policy plan\n"
            "wheel-reconcile  dry call to Wheel reconciliation\n"
            "wheel-submit-paper  separately gated PAPER submission path\n"
        )
        return 0

    command, rest = args[0], args[1:]
    if command == "preflight":
        from opaca.preflight import main as preflight_main

        return preflight_main(rest)
    if command == "intake-demo":
        from opaca.intake.cli import main as intake_demo_main

        return intake_demo_main(rest)
    if command.startswith("wheel-"):
        from opaca.wheel.cli import main as wheel_main

        return wheel_main([command, *rest])

    sys.stderr.write(f"unknown command {command!r}; try --help\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
