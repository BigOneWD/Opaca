"""Read-only command-line demo for AI obligation intake."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from opaca.intake.models import (
    MAX_DOCUMENT_CHARS,
    MAX_MODEL_RESPONSE_CHARS,
    IntakeBlockedError,
)
from opaca.intake.provider import (
    ExtractionUnavailableError,
    FixtureObligationExtractor,
    ObligationExtractor,
    OpenAICompatibleObligationExtractor,
)
from opaca.intake.validation import parse_and_validate_extraction, require_effective_obligations

_CANONICAL_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _parse_canonical_date(value: str) -> date:
    if _CANONICAL_DATE_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("--as-of must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--as-of must be YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m opaca intake-demo",
        description=(
            "Read-only obligation extraction demo. When an openai-compatible endpoint is "
            "non-local, the supplied document is sent to that configured endpoint."
        ),
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--as-of", required=True, type=_parse_canonical_date)
    parser.add_argument(
        "--provider",
        choices=("fixture", "openai-compatible"),
        required=True,
    )
    parser.add_argument("--fixture-json", type=Path)
    return parser


def _format_date(value: date | None) -> str:
    return "n/a" if value is None else value.isoformat()


def _safe_cli_text(value: str) -> str:
    """Encode untrusted text so it cannot add lines or terminal controls."""
    return value.encode("unicode_escape").decode("ascii").replace('"', '\\"')


def _read_text_bounded(path: Path, *, max_chars: int, label: str) -> str:
    with path.open("r", encoding="utf-8") as stream:
        value = stream.read(max_chars + 1)
    if len(value) > max_chars:
        raise ExtractionUnavailableError(f"{label} exceeds size limit")
    return value


def _extractor(provider: str, input_path: Path, fixture_json: Path | None) -> ObligationExtractor:
    if provider == "fixture":
        fixture_path = fixture_json or input_path.with_name("fixture_extraction.json")
        return FixtureObligationExtractor(
            raw_json=_read_text_bounded(
                fixture_path,
                max_chars=MAX_MODEL_RESPONSE_CHARS,
                label="fixture response",
            )
        )

    base_url = os.environ.get("OPACA_LLM_BASE_URL", "").strip()
    model = os.environ.get("OPACA_LLM_MODEL", "").strip()
    if not base_url or not model:
        raise ExtractionUnavailableError("provider configuration unavailable")

    return OpenAICompatibleObligationExtractor(
        base_url=base_url,
        model=model,
        api_key=os.environ.get("OPACA_LLM_API_KEY", ""),
    )


def _write_no_mutation_notice() -> None:
    sys.stdout.write("BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    input_path: Path = args.input
    as_of: date = args.as_of

    try:
        document = _read_text_bounded(
            input_path,
            max_chars=MAX_DOCUMENT_CHARS,
            label="document",
        )
        extractor = _extractor(args.provider, input_path, args.fixture_json)
        raw_json = extractor.extract(document, as_of=as_of)
    except ExtractionUnavailableError:
        sys.stdout.write("INTAKE UNAVAILABLE\n")
        _write_no_mutation_notice()
        return 1

    try:
        result = parse_and_validate_extraction(document, raw_json, as_of=as_of)
        require_effective_obligations(result)
    except IntakeBlockedError as exc:
        sys.stdout.write("INTAKE BLOCKED\n")
        sys.stdout.write(f"BLOCK REASON: {exc}\n")
        _write_no_mutation_notice()
        return 1

    out = sys.stdout
    out.write("AI OBLIGATION INTAKE\n")
    out.write(f"PROVIDER: {extractor.provider_name}\n")
    if extractor.provider_name == "fixture":
        out.write("OFFLINE FIXTURE: NOT A REAL AI CALL\n")
    out.write(f"SOURCE SHA256: {result.source_sha256}\n")

    for index, candidate in enumerate(result.candidates, start=1):
        out.write(f"\nCANDIDATE {index}\n")
        out.write(f"STATUS: {candidate.certainty.value}\n")
        out.write(f"NAME: {_safe_cli_text(candidate.name)}\n")
        out.write(f"AMOUNT: {candidate.amount if candidate.amount is not None else 'n/a'}\n")
        out.write(f"STATED DUE DATE: {_format_date(candidate.stated_due_date)}\n")
        out.write(f"EFFECTIVE DUE DATE: {_format_date(candidate.effective_due_date)}\n")
        out.write(
            f"RESERVED CONSERVATIVELY: {'YES' if candidate.reserved_conservatively else 'NO'}\n"
        )
        if candidate.certainty.value == "UNCERTAIN":
            out.write("HUMAN REVIEW: REQUIRED\n")
        out.write(f'EVIDENCE: "{_safe_cli_text(candidate.source_excerpt)}"\n')

    out.write(f"\nUNCERTAIN RESERVED AMOUNT: {result.uncertain_reserved_amount}\n")
    out.write(f"TRADE BLOCKED: {'YES' if result.trade_blocked else 'NO'}\n")
    for reason in result.block_reasons:
        out.write(f"BLOCK REASON: {reason}\n")
    _write_no_mutation_notice()
    return 0
