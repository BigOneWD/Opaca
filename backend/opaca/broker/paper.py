"""Paper-only environment gate. Live execution is impossible in this phase."""

from __future__ import annotations

import os

from opaca.broker.errors import PaperEnvironmentError
from opaca.broker.gateway import LIVE_ENDPOINT, PAPER_ENDPOINT

ENV_KEY_ID = "APCA_API_KEY_ID"
ENV_SECRET = "APCA_API_SECRET_KEY"


def load_paper_credentials() -> tuple[str, str]:
    key_id = os.environ.get(ENV_KEY_ID, "").strip()
    secret = os.environ.get(ENV_SECRET, "").strip()
    if not key_id or not secret:
        raise PaperEnvironmentError("paper credentials are not present in the environment")
    return key_id, secret


def client_base_url(client: object) -> str:
    base = getattr(client, "_base_url", "") or ""
    return str(getattr(base, "value", base)).rstrip("/")


def verify_paper_client(client: object) -> str:
    """Verify the constructed client's endpoint. Do not trust a config flag alone."""
    url = client_base_url(client)
    if url.startswith(LIVE_ENDPOINT):
        raise PaperEnvironmentError("live Alpaca endpoint is forbidden")
    if not url.startswith(PAPER_ENDPOINT):
        raise PaperEnvironmentError(
            f"paper endpoint not confirmed; expected prefix {PAPER_ENDPOINT!r}"
        )
    paper_flag = getattr(client, "_paper", None)
    if paper_flag is False:
        raise PaperEnvironmentError("TradingClient paper flag is false")
    return url
