import json
from datetime import date
from typing import Any
from urllib.request import Request

from opaca.intake.provider import OpenAICompatibleObligationExtractor


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


class FakeUrlOpen:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.request: Request | None = None
        self.timeout: float | None = None

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        self.request = request
        self.timeout = timeout
        return FakeResponse(self.payload)


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
    extractor = OpenAICompatibleObligationExtractor(
        base_url="http://127.0.0.1:8080/v1/",
        model="local-model",
        api_key="super-secret",
        opener=opener,
    )

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
