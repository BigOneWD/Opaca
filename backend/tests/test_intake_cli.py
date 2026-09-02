import inspect
import json
from datetime import date
from pathlib import Path

import pytest

from opaca.__main__ import main
from opaca.intake import cli as intake_cli
from opaca.intake.provider import (
    ExtractionUnavailableError,
    OpenAICompatibleObligationExtractor,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "intake"


def test_fixture_intake_demo_is_explicit_and_read_only(capsys) -> None:
    input_path = _FIXTURES / "messy_obligations.md"

    rc = main(
        [
            "intake-demo",
            "--input",
            str(input_path),
            "--as-of",
            "2026-09-02",
            "--provider",
            "fixture",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "AI OBLIGATION INTAKE" in captured.out
    assert "PROVIDER: fixture" in captured.out
    assert "STATUS: CONFIRMED" in captured.out
    assert "STATUS: UNCERTAIN" in captured.out
    assert "UNCERTAIN RESERVED AMOUNT: 80000.00" in captured.out
    assert "TRADE BLOCKED: NO" in captured.out
    assert "HUMAN REVIEW: REQUIRED" in captured.out
    assert "BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND" in captured.out


def test_unquantified_fixture_surfaces_block_reason(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "unquantified_obligation.md"
    input_path.write_text(
        "A regulatory payment is due this month; amount pending assessment.\n",
        encoding="utf-8",
    )

    rc = main(
        [
            "intake-demo",
            "--input",
            str(input_path),
            "--as-of",
            "2026-09-02",
            "--provider",
            "fixture",
            "--fixture-json",
            str(_FIXTURES / "unquantified_extraction.json"),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "TRADE BLOCKED: YES" in captured.out
    assert "BLOCK REASON: UNQUANTIFIED_OBLIGATION" in captured.out
    assert "BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND" in captured.out


def test_evidence_mismatch_becomes_intake_blocked(tmp_path: Path, capsys) -> None:
    input_path = _FIXTURES / "messy_obligations.md"
    payload = json.loads((_FIXTURES / "fixture_extraction.json").read_text(encoding="utf-8"))
    payload["candidates"][0]["source_excerpt"] = "This evidence is not in the source document."
    fixture_path = tmp_path / "evidence_mismatch.json"
    fixture_path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        rc = main(
            [
                "intake-demo",
                "--input",
                str(input_path),
                "--as-of",
                "2026-09-02",
                "--provider",
                "fixture",
                "--fixture-json",
                str(fixture_path),
            ]
        )
    except Exception as exc:  # pragma: no cover - RED guard against leaked internals
        pytest.fail(f"intake-demo leaked {type(exc).__name__}: {exc}")

    captured = capsys.readouterr()
    assert rc == 1
    assert "INTAKE BLOCKED" in captured.out
    assert "MODEL_EVIDENCE_MISMATCH" in captured.out
    assert "BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND" in captured.out


def test_provider_failure_is_unavailable_and_never_leaks_api_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    secret = "cli-super-secret"

    def fail_extract(
        self: OpenAICompatibleObligationExtractor,
        document: str,
        *,
        as_of: date,
    ) -> str:
        del self, document, as_of
        raise ExtractionUnavailableError(f"transport failed with {secret}")

    monkeypatch.setattr(OpenAICompatibleObligationExtractor, "extract", fail_extract)
    monkeypatch.setenv("OPACA_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("OPACA_LLM_MODEL", "local-model")
    monkeypatch.setenv("OPACA_LLM_API_KEY", secret)

    try:
        rc = main(
            [
                "intake-demo",
                "--input",
                str(_FIXTURES / "messy_obligations.md"),
                "--as-of",
                "2026-09-02",
                "--provider",
                "openai-compatible",
            ]
        )
    except BaseException as exc:  # pragma: no cover - RED guard against argparse/tracebacks
        pytest.fail(f"intake-demo leaked {type(exc).__name__}: {exc}")

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert rc == 1
    assert "INTAKE UNAVAILABLE" in captured.out
    assert secret not in combined
    assert "BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND" in captured.out


def test_intake_help_discloses_document_delivery_for_non_local_endpoints(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["intake-demo", "--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    help_text = captured.out.lower()
    assert "non-local" in help_text
    assert "supplied document" in help_text


def test_intake_demo_source_has_no_broker_mutation_gateway() -> None:
    source = inspect.getsource(intake_cli)
    forbidden = (
        "TradingClient",
        "submit_order",
        "cancel_order",
        "live-paper-mutation",
        "PaperMutation",
    )
    assert all(token not in source for token in forbidden)
