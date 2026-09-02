"""OpenAI-compatible obligation extraction boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, cast
from urllib.request import Request, urlopen

_SYSTEM_PROMPT = """You extract corporate cash obligations from supplied text.
Return exactly one JSON object and nothing else: no Markdown fence and no prose.
Use this exact schema:
{
  "document_summary": "short factual summary",
  "candidates": [
    {
      "name": "obligation name",
      "amount": "positive decimal string or null",
      "due_date": "YYYY-MM-DD or null",
      "currency": "USD",
      "certainty": "CONFIRMED or UNCERTAIN",
      "uncertainty_reason": "reason or null",
      "source_excerpt": "exact contiguous source text"
    }
  ]
}
Each candidate must use exactly those fields. Use certainty CONFIRMED only when the
amount and due_date are explicitly stated, currency is USD, and no uncertainty
remains; then uncertainty_reason must be null. Use UNCERTAIN when an obligation is
plausible but its amount or due_date is missing or uncertain; null is allowed for a
missing amount or due_date and uncertainty_reason must explain what is unresolved.
source_excerpt must be an exact excerpt copied from the supplied document. Do not guess,
infer, calculate, or normalize a missing amount or due date from conventions such as net
terms unless the source contains every required anchor. Do not make trading decisions,
authorize orders, size trades, or invent missing obligation facts.
"""
_MAX_DOCUMENT_CHARS = 50_000


class ExtractionUnavailableError(RuntimeError):
    """Raised when obligation extraction cannot produce a usable raw response."""


class ObligationExtractor(Protocol):
    """Structural interface shared by real and fixture extraction providers."""

    @property
    def provider_name(self) -> str: ...

    def extract(self, document: str, *, as_of: date) -> str: ...


class _ResponseLike(Protocol):
    status: int

    def __enter__(self) -> _ResponseLike: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...

    def read(self) -> bytes: ...


class _UrlOpenLike(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> _ResponseLike: ...


def _urlopen(request: Request, *, timeout: float) -> _ResponseLike:
    return cast(_ResponseLike, urlopen(request, timeout=timeout))


def _parse_response_payload(raw_body: bytes) -> dict[str, object]:
    try:
        decoded_text = raw_body.decode("utf-8")
        payload = json.loads(decoded_text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractionUnavailableError("provider returned malformed JSON") from exc

    if not isinstance(payload, dict):
        raise ExtractionUnavailableError("provider response must be a JSON object")
    return cast(dict[str, object], payload)


def _assistant_content(payload: dict[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ExtractionUnavailableError("provider response missing choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ExtractionUnavailableError("provider response choice is invalid")
    first_object = cast(dict[str, object], first)
    message = first_object.get("message")
    if not isinstance(message, dict):
        raise ExtractionUnavailableError("provider response missing message")
    message_object = cast(dict[str, object], message)
    content = message_object.get("content")
    if not isinstance(content, str):
        raise ExtractionUnavailableError("provider response missing assistant content")
    return content


@dataclass(frozen=True)
class FixtureObligationExtractor:
    """Deterministic offline extractor that is always visibly fixture-backed."""

    raw_json: str
    provider_name: str = field(default="fixture", init=False)

    def extract(self, document: str, *, as_of: date) -> str:
        del document, as_of
        return self.raw_json


@dataclass(frozen=True)
class OpenAICompatibleObligationExtractor:
    """Extract raw obligation JSON from an OpenAI-compatible chat endpoint."""

    base_url: str
    model: str
    api_key: str = field(repr=False)
    opener: _UrlOpenLike = field(default=_urlopen, repr=False)
    provider_name: str = "openai-compatible"

    def extract(self, document: str, *, as_of: date) -> str:
        if len(document) > _MAX_DOCUMENT_CHARS:
            raise ExtractionUnavailableError("document exceeds 50000 character limit")

        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"As of {as_of.isoformat()}:\n\n{document}",
                },
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with self.opener(request, timeout=30.0) as response:
                if response.status < 200 or response.status >= 300:
                    raise ExtractionUnavailableError("provider returned non-success status")
                raw_body = response.read()
        except ExtractionUnavailableError:
            raise
        except (TimeoutError, OSError):
            raise ExtractionUnavailableError("provider transport unavailable") from None

        payload = _parse_response_payload(raw_body)
        return _assistant_content(payload)
