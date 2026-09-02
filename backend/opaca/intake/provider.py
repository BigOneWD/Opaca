"""OpenAI-compatible obligation extraction boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, cast
from urllib.request import Request, urlopen

_SYSTEM_PROMPT = """You extract corporate cash obligations from supplied text.
Return exactly one JSON object with document_summary and candidates. Do not make
trading decisions, authorize orders, or invent missing obligation facts.
"""


class _ResponseLike(Protocol):
    status: int

    def __enter__(self) -> "_ResponseLike": ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...

    def read(self) -> bytes: ...


class _UrlOpenLike(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> _ResponseLike: ...


def _urlopen(request: Request, *, timeout: float) -> _ResponseLike:
    return cast(_ResponseLike, urlopen(request, timeout=timeout))


@dataclass(frozen=True)
class OpenAICompatibleObligationExtractor:
    """Extract raw obligation JSON from an OpenAI-compatible chat endpoint."""

    base_url: str
    model: str
    api_key: str = field(repr=False)
    opener: _UrlOpenLike = field(default=_urlopen, repr=False)
    provider_name: str = "openai-compatible"

    def extract(self, document: str, *, as_of: date) -> str:
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
        with self.opener(request, timeout=30.0) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if not isinstance(payload, dict):
            raise ValueError("provider response must be a JSON object")
        response_object = cast(dict[str, object], payload)
        choices = response_object.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("provider response missing choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise ValueError("provider response choice is invalid")
        first_object = cast(dict[str, object], first)
        message = first_object.get("message")
        if not isinstance(message, dict):
            raise ValueError("provider response missing message")
        message_object = cast(dict[str, object], message)
        content = message_object.get("content")
        if not isinstance(content, str):
            raise ValueError("provider response missing assistant content")
        return content
