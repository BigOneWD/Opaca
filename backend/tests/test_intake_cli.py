from pathlib import Path

from opaca.__main__ import main

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
