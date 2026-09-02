import ast
import inspect
import json
from datetime import date
from pathlib import Path

import pytest
from opaca.__main__ import main
from opaca.intake import cli as intake_cli
from opaca.intake.models import MAX_DOCUMENT_CHARS, MAX_MODEL_RESPONSE_CHARS
from opaca.intake.provider import (
    ExtractionUnavailableError,
    OpenAICompatibleObligationExtractor,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "intake"


def test_fixture_intake_demo_is_explicit_and_read_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    assert rc == 1
    assert captured.err == ""
    assert "INTAKE BLOCKED" in captured.out
    assert "COMPLETENESS_REVIEW_REQUIRED" in captured.out
    assert "BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND" in captured.out


def test_fixture_intake_demo_requires_completeness_review(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(
        [
            "intake-demo",
            "--input",
            str(_FIXTURES / "messy_obligations.md"),
            "--as-of",
            "2026-09-02",
            "--provider",
            "fixture",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "INTAKE BLOCKED" in captured.out
    assert "COMPLETENESS_REVIEW_REQUIRED" in captured.out
    assert "TRADE BLOCKED: NO" not in captured.out
    assert "BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND" in captured.out


def test_unquantified_fixture_surfaces_block_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    assert rc == 1
    assert captured.err == ""
    assert "INTAKE BLOCKED" in captured.out
    assert "UNQUANTIFIED_OBLIGATION" in captured.out
    assert "BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND" in captured.out


def test_evidence_mismatch_becomes_intake_blocked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    capsys: pytest.CaptureFixture[str],
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


def test_candidate_control_characters_are_escaped_for_cli_output() -> None:
    value = 'pay\nSTATUS: CONFIRMED\r\t\b\x1b[31m"\u2028\u2029'

    escaped = intake_cli._safe_cli_text(value)

    assert "\x1b" not in escaped
    assert "pay\\nSTATUS: CONFIRMED" in escaped
    assert "\\r\\t\\x08\\x1b[31m" in escaped
    assert '\\"' in escaped
    assert "\\u2028\\u2029" in escaped


@pytest.mark.parametrize("missing_name", ["OPACA_LLM_BASE_URL", "OPACA_LLM_MODEL"])
def test_missing_real_provider_config_is_intake_unavailable(
    missing_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPACA_LLM_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("OPACA_LLM_MODEL", "local-model")
    monkeypatch.delenv(missing_name, raising=False)
    monkeypatch.delenv("OPACA_LLM_API_KEY", raising=False)

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
    except BaseException as exc:  # pragma: no cover - RED guard against config tracebacks
        pytest.fail(f"intake-demo leaked {type(exc).__name__}: {exc}")

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == ""
    assert "INTAKE UNAVAILABLE" in captured.out
    assert "BROKER MUTATION: NOT AVAILABLE IN THIS COMMAND" in captured.out


def test_intake_help_discloses_document_delivery_for_non_local_endpoints(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["intake-demo", "--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    help_text = captured.out.lower()
    assert "non-local" in help_text
    assert "supplied document" in help_text


def test_bounded_text_reader_reads_only_one_character_past_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingReader:
        def __init__(self) -> None:
            self.requested_size: int | None = None

        def __enter__(self) -> "RecordingReader":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self, size: int) -> str:
            self.requested_size = size
            return "x" * size

    reader = RecordingReader()

    def open_reader(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> RecordingReader:
        del self, mode, buffering, encoding, errors, newline
        return reader

    monkeypatch.setattr(Path, "open", open_reader)
    bounded_reader = getattr(intake_cli, "_read_text_bounded", None)
    assert callable(bounded_reader)

    with pytest.raises(ExtractionUnavailableError, match="document exceeds"):
        bounded_reader(Path("document.txt"), max_chars=5, label="document")

    assert reader.requested_size == 6


def test_intake_demo_uses_bounded_reads_for_document_and_fixture(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, int, Path]] = []
    document = "Payment of USD 10.00 is due by 12 September 2026."
    fixture = json.dumps(
        {
            "document_summary": "payment",
            "candidates": [
                {
                    "name": "payment",
                    "amount": "10.00",
                    "due_date": "2026-09-12",
                    "currency": "USD",
                    "certainty": "CONFIRMED",
                    "uncertainty_reason": None,
                    "source_excerpt": document,
                }
            ],
        }
    )

    def bounded_reader(path: Path, *, max_chars: int, label: str) -> str:
        calls.append((label, max_chars, path))
        return document if label == "document" else fixture

    monkeypatch.setattr(intake_cli, "_read_text_bounded", bounded_reader)

    rc = main(
        [
            "intake-demo",
            "--input",
            "/private/tmp/document.txt",
            "--as-of",
            "2026-09-02",
            "--provider",
            "fixture",
            "--fixture-json",
            "/private/tmp/fixture.json",
        ]
    )
    capsys.readouterr()

    assert rc == 1
    assert calls == [
        ("document", MAX_DOCUMENT_CHARS, Path("/private/tmp/document.txt")),
        ("fixture response", MAX_MODEL_RESPONSE_CHARS, Path("/private/tmp/fixture.json")),
    ]


def test_intake_demo_ast_has_no_broker_mutation_gateway() -> None:
    tree = ast.parse(inspect.getsource(intake_cli))
    forbidden_modules = {
        "opaca.broker.mutation",
        "opaca.execution.gateway",
        "opaca.execution.service",
    }
    forbidden_symbols = {
        "TradingClient",
        "submit_order",
        "cancel_order",
        "PaperMutation",
    }

    imported_modules: set[str] = set()
    referenced_symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)
            referenced_symbols.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            referenced_symbols.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced_symbols.add(node.attr)

    assert imported_modules.isdisjoint(forbidden_modules)
    assert referenced_symbols.isdisjoint(forbidden_symbols)
