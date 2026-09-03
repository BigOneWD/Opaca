"""RED-phase contracts for Wheel CLI wiring and mutation separation."""

from __future__ import annotations

import pytest
from opaca.__main__ import main
from opaca.wheel import cli


@pytest.mark.parametrize("command", ["wheel-probe", "wheel-plan", "wheel-reconcile"])
def test_read_only_wheel_commands_are_available_without_network(
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([command]) == 0
    output = capsys.readouterr().out
    assert "DRY_RUN / SIMULATED" in output
    assert "MUTATION_READY: NO" in output


def test_help_lists_separate_wheel_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    for command in (
        "wheel-probe",
        "wheel-plan",
        "wheel-reconcile",
        "wheel-submit-paper",
    ):
        assert command in output


def test_submit_command_requires_explicit_paper_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "_paper_gateway_factory", lambda: calls.append("factory"))

    assert main(["wheel-submit-paper"]) != 0
    assert calls == []
    assert "EXPLICIT PAPER MUTATION OPT-IN REQUIRED" in capsys.readouterr().out


def test_wrong_environment_never_reaches_submit_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "false")
    monkeypatch.setattr(cli, "_paper_gateway_factory", lambda: calls.append("factory"))

    assert main(["wheel-submit-paper", "--confirm-paper-mutation"]) != 0
    assert calls == []


def test_current_indicative_preflight_blocker_suppresses_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "true")
    monkeypatch.setattr(cli, "_paper_gateway_factory", lambda: calls.append("factory"))

    assert main(["wheel-submit-paper", "--confirm-paper-mutation"]) != 0
    assert calls == []
    assert "MUTATION_READY: NO" in capsys.readouterr().out


def test_read_only_commands_do_not_construct_mutation_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "_paper_gateway_factory", lambda: calls.append("factory"))

    for command in ("wheel-probe", "wheel-plan", "wheel-reconcile"):
        assert main([command]) == 0
    assert calls == []
