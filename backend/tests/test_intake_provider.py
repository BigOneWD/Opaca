import json
import traceback
from datetime import date
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any, ClassVar
from urllib.request import Request

import pytest
from opaca.intake.provider import (
    ExtractionUnavailableError,
    FixtureObligationExtractor,
    ObligationExtractor,
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


class RaisingUrlOpen:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        del request, timeout
        raise self.error


class RedirectSourceHandler(BaseHTTPRequestHandler):
    authorization: ClassVar[str | None] = None
    redirect_location: ClassVar[str] = ""

    def do_POST(self) -> None:
        type(self).authorization = self.headers.get("Authorization")
        self.send_response(302)
        self.send_header("Location", type(self).redirect_location)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class RedirectTargetHandler(BaseHTTPRequestHandler):
    requests: ClassVar[int] = 0
    authorization: ClassVar[str | None] = None

    def _respond(self) -> None:
        type(self).requests += 1
        type(self).authorization = self.headers.get("Authorization")
        body = json.dumps(
            {"choices": [{"message": {"content": '{"document_summary":"x","candidates":[]}'}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _extractor(
    opener: FakeUrlOpen | RawUrlOpen | TimeoutUrlOpen | RaisingUrlOpen,
    *,
    api_key: str = "super-secret",
) -> OpenAICompatibleObligationExtractor:
    return OpenAICompatibleObligationExtractor(
        base_url="http://127.0.0.1:8080/v1/",
        model="local-model",
        api_key=api_key,
        opener=opener,
    )


def _extract_through_protocol(extractor: ObligationExtractor, document: str) -> str:
    return extractor.extract(document, as_of=date(2026, 9, 2))


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

    request_data = opener.request.data
    assert isinstance(request_data, bytes)
    body: dict[str, Any] = json.loads(request_data)
    assert body["model"] == "local-model"
    assert body["temperature"] == 0
    assert body["messages"][0]["role"] == "system"
    system_prompt = body["messages"][0]["content"]
    assert isinstance(system_prompt, str)
    assert "document_summary" in system_prompt
    assert "candidates" in system_prompt
    assert "amount" in system_prompt
    assert "due_date" in system_prompt
    assert "currency" in system_prompt
    assert "certainty" in system_prompt
    assert "source_excerpt" in system_prompt
    assert "CONFIRMED" in system_prompt
    assert "UNCERTAIN" in system_prompt
    assert "exact" in system_prompt.lower()
    assert "do not guess" in system_prompt.lower()
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


def test_oversized_model_response_becomes_intake_unavailable() -> None:
    oversized_content = "x" * 100_001
    body = json.dumps({"choices": [{"message": {"content": oversized_content}}]}).encode("utf-8")
    extractor = _extractor(RawUrlOpen(body))

    with pytest.raises(ExtractionUnavailableError, match="response exceeds"):
        extractor.extract("No obligations.", as_of=date(2026, 9, 2))


def test_provider_error_never_leaks_api_key() -> None:
    secret = "super-secret"
    opener = TimeoutUrlOpen(f"transport failed while using {secret}")
    extractor = _extractor(opener, api_key=secret)

    with pytest.raises(ExtractionUnavailableError) as exc_info:
        extractor.extract("No obligations.", as_of=date(2026, 9, 2))

    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)


def test_provider_traceback_never_leaks_api_key_from_exception_cause() -> None:
    secret = "super-secret"
    opener = TimeoutUrlOpen(f"transport failed while using {secret}")
    extractor = _extractor(opener, api_key=secret)

    with pytest.raises(ExtractionUnavailableError) as exc_info:
        extractor.extract("No obligations.", as_of=date(2026, 9, 2))

    formatted = "".join(traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb))
    assert secret not in formatted


def test_provider_does_not_follow_redirect_to_another_origin_with_bearer() -> None:
    source_server = HTTPServer(("127.0.0.1", 0), RedirectSourceHandler)
    target_server = HTTPServer(("127.0.0.1", 0), RedirectTargetHandler)
    RedirectSourceHandler.redirect_location = (
        f"http://127.0.0.1:{target_server.server_port}/redirected"
    )
    RedirectSourceHandler.authorization = None
    RedirectTargetHandler.requests = 0
    RedirectTargetHandler.authorization = None
    source_thread = Thread(target=source_server.serve_forever, daemon=True)
    target_thread = Thread(target=target_server.serve_forever, daemon=True)
    source_thread.start()
    target_thread.start()

    try:
        extractor = OpenAICompatibleObligationExtractor(
            base_url=f"http://127.0.0.1:{source_server.server_port}/v1",
            model="local-model",
            api_key="redirect-secret",
        )

        with pytest.raises(ExtractionUnavailableError):
            extractor.extract("No obligations.", as_of=date(2026, 9, 2))
    finally:
        source_server.shutdown()
        target_server.shutdown()
        source_server.server_close()
        target_server.server_close()
        source_thread.join()
        target_thread.join()

    assert RedirectSourceHandler.authorization == "Bearer redirect-secret"
    assert RedirectTargetHandler.requests == 0
    assert RedirectTargetHandler.authorization is None


@pytest.mark.parametrize(
    "error",
    [
        IncompleteRead(b"partial response"),
        ValueError("malformed provider URL"),
        TypeError("invalid provider request configuration"),
    ],
    ids=["incomplete-read", "value-error", "type-error"],
)
def test_expected_provider_failures_become_sanitized_unavailable(
    error: Exception,
) -> None:
    extractor = _extractor(RaisingUrlOpen(error))

    with pytest.raises(ExtractionUnavailableError) as exc_info:
        extractor.extract("No obligations.", as_of=date(2026, 9, 2))

    assert str(exc_info.value) == "provider transport unavailable"
    assert "partial response" not in str(exc_info.value)


def test_malformed_provider_url_becomes_unavailable() -> None:
    extractor = OpenAICompatibleObligationExtractor(
        base_url="http://[malformed",
        model="local-model",
        api_key="super-secret",
    )

    with pytest.raises(ExtractionUnavailableError) as exc_info:
        extractor.extract("No obligations.", as_of=date(2026, 9, 2))

    assert str(exc_info.value) == "provider transport unavailable"


def test_fixture_extractor_is_explicit_and_satisfies_protocol() -> None:
    raw_json = '{"document_summary":"fixture","candidates":[]}'
    extractor = FixtureObligationExtractor(raw_json=raw_json)

    assert extractor.provider_name == "fixture"
    assert _extract_through_protocol(extractor, "Synthetic fixture text.") == raw_json


def test_oversized_fixture_response_becomes_intake_unavailable() -> None:
    extractor = FixtureObligationExtractor(raw_json="x" * 100_001)

    with pytest.raises(ExtractionUnavailableError, match="response exceeds"):
        extractor.extract("No obligations.", as_of=date(2026, 9, 2))


def test_oversized_fixture_document_becomes_intake_unavailable() -> None:
    extractor = FixtureObligationExtractor(raw_json='{"document_summary":"fixture"}')

    with pytest.raises(ExtractionUnavailableError, match="document exceeds"):
        extractor.extract("x" * 50_001, as_of=date(2026, 9, 2))
