"""Read-only command-line demo for AI obligation intake."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from opaca.intake.provider import FixtureObligationExtractor
from opaca.intake.validation import parse_and_validate_extraction


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m opaca intake-demo")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--provider", choices=("fixture",), required=True)
    parser.add_argument("--fixture-json", type=Path)
    return parser


def _format_date(value: date | None) -> str:
    return "n/a" if value is None else value.isoformat()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    input_path: Path = args.input
    as_of = date.fromisoformat(args.as_of)
    document = input_path.read_text(encoding="utf-8")

    fixture_json: Path = args.fixture_json or input_path.with_name("fixture_extraction.json")
    extractor = FixtureObligationExtractor(raw_json=fixture_json.read_text(encoding="utf-8"))
    raw_json = extractor.extract(document, as_of=as_of)
    result = parse_and_validate_extraction(document, raw_json, as_of=as_of)

    out = sys.stdout
    out.write("AI OBLIGATION INTAKE\n")
    out.write(f"PROVIDER: {extractor.provider_name}\n")
    out.write(f"SOURCE SHA256: {result.source_sha256}\n")

    for index, candidate in enumerate(result.candidates, start=1):
        out.write(f"\nCANDIDATE {index}\n")
        out.write(f"STATUS: {candidate.certainty.value}\n")
        out.write(f"NAME: {candidate.name}\n")
        out.write(f"AMOUNT: {candidate.amount if candidate.amount is not None else 'n/a'}\n")
        out.write(f"STATED DUE DATE: {_format_date(candidate.stated_due_date)}\n")
        out.write(f"EFFECTIVE DUE DATE: {_format_date(candidate.effective_due_date)}\n")
        out.write(
            "RESERVED CONSERVATIVELY: "
            f"{'YES' if candidate.reserved_conservatively else 'NO'}\n"
        )
        if candidate.certainty.value == "UNCERTAIN":
            out.write("HUMAN REVIEW: REQUIRED\n")
        out.write(f'EVIDENCE: "{candidate.source_excerpt}"\n')

    out.write(f"\nUNCERTAIN RESERVED AMOUNT: {result.uncertain_reserved_amount}\n")
    out.write(f"TRADE BLOCKED: {'YES' if result.trade_blocked else 'NO'}\n")
    out.write("BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND\n")
    return 0
