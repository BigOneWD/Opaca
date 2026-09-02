import json
from datetime import date
from typing import Any
from urllib.request import Request

import pytest

from opaca.intake.provider import (
    ExtractionUnavailableError,
    OpenAICompatibleObligationExtractor,
)


class FakeResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class RawResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> "RawResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class FakeUrlOpen:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.request: Request | None = None
        self.timeout: float | None = None
        self.calls = 0

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        self.calls += 1
        self.request = request
        self.timeout = timeout
        return FakeResponse(self.payload, status=self.status)


class RawUrlOpen:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __call__(self, request: Request, *, timeout: float) -> RawResponse:
        del request, timeout
        return RawResponse(self.body)


class TimeoutUrlOpen:
    def __init__(self, message: str = "timed out") -> None:
        self.message = message
        self.calls = 0

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        del request, timeout
        self.calls += 1
        raise TimeoutError(self.message)


def _extractor(
    opener: FakeUrlOpen | RawUrlOpen | TimeoutUrlOpen,
    *,
    api_key: str = "super-secret",
) -> OpenAICompatibleObligationExtractor:
    return OpenAICompatibleObligationExtractor(
        base_url="http://127.0.0.1:8080/v1/",
        model="local-model",
        api_key=api_key,
        opener=opener,
    )


def test_openai_compatible_provider_builds_fixed_request_and_returns_json() -> None:
    assistant_json = '{"document_summary":"none","candidates":[]}'
    opener = FakeUrlOpen(
        {
            "choices": [
                {
                    "message": {
                        "content": assistant_json,
                    }
                }
            ]
        }
    )
    extractor = _extractor(opener)

    raw = extractor.extract("No obligations.", as_of=date(2026, 9, 2))

    assert raw == assistant_json
    assert opener.request is not None
    assert opener.request.full_url == "http://127.0.0.1:8080/v1/chat/completions"
    assert opener.timeout == 30.0

    body: dict[str, Any] = json.loads(opener.request.data or b"{}")
    assert body["model"] == "local-model"
    assert body["temperature"] == 0
    assert body["messages"][0]["role"] == "system"
    assert "obligation" in body["messages"][0]["content"].lower()
    assert body["messages"][1]["role"] == "user"
    assert "No obligations." in body["messages"][1]["content"]
    assert opener.request.get_header("Authorization") == "Bearer super-secret"
    assert "super-secret" not in repr(extractor)


def test_timeout_becomes_intake_unavailable() -> None:
    opener = TimeoutUrlOpen()
    extractor = _extractor(opener)

    with pytest.raises(ExtractionUnavailableError):
        extractor.extract("No obligations.", as_of=date(2026, 9, 2))

    assert opener.calls == 1


def test_non_2xx_becomes_intake_unavailable() -> None:
    opener = FakeUrlOpen({"error": "upstream unavailable"}, status=503)
    extractor = _extractor(opener)

    with pytest.raises(ExtractionUnavailableError):
        extractor.extract("No obligations.", as_of=date(2026, 9, 2))


def test_malformed_transport_json_becomes_intake_unavailable() -> None:
    extractor = _extractor(RawUrlOpen(b"not-json"))

    with pytest.raises(ExtractionUnavailableError):
        extractor.extract("No obligations.", as_of=date(2026, 9, 2))


def test_missing_assistant_content_becomes_intake_unavailable() -> None:
    opener = FakeUrlOpen({"choices": [{"message": {}}]})
    extractor = _extractor(opener)

    with pytest.raises(ExtractionUnavailableError):
        extractor.extract("No obligations.", as_of=date(2026, 9, 2))


def test_oversize_document_never_calls_network() -> None:
    opener = FakeUrlOpen({"choices": [{"message": {"content": "{}"}}]})
    extractor = _extractor(opener)

    with pytest.raises(ExtractionUnavailableError):
        extractor.extract("x" * 50_001, as_of=date(2026, 9, 2))

    assert opener.calls == 0


def test_provider_error_never_leaks_api_key() -> None:
    secret = "super-secret"
    opener = TimeoutUrlOpen(f"transport failed while using {secret}")
    extractor = _extractor(opener, api_key=secret)

    with pytest.raises(ExtractionUnavailableError) as exc_info:
        extractor.extract("No obligations.", as_of=date(2026, 9, 2))

    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)
