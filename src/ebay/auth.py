"""eBay OAuth client-credentials authentication.

Tokens are kept in memory only. A token is refreshed shortly before expiry and
once more after a 401, so long scheduler runs cannot drift past expiry.
"""

from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from src.config import Settings

SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayAuthError(RuntimeError):
    """Authentication failed or credentials are not configured."""


@dataclass(frozen=True)
class Token:
    access_token: str
    expires_at: float
    token_type: str = "Bearer"

    def valid(self, *, now: float | None = None, skew_seconds: float = 120.0) -> bool:
        current = time.time() if now is None else now
        return bool(self.access_token) and current + skew_seconds < self.expires_at


class EbayAuth:
    def __init__(
        self,
        settings: Settings,
        http: httpx.Client,
        *,
        on_call: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._http = http
        self._on_call = on_call
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Token | None = None

    @property
    def token_url(self) -> str:
        host = (
            "https://api.sandbox.ebay.com"
            if self._settings.ebay_environment == "sandbox"
            else "https://api.ebay.com"
        )
        return f"{host}/identity/v1/oauth2/token"

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def get_token(self, *, force: bool = False) -> str:
        with self._lock:
            if not force and self._token is not None and self._token.valid(now=self._clock()):
                return self._token.access_token
            if not self._settings.ebay_client_id or not self._settings.ebay_client_secret:
                raise EbayAuthError(
                    "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are required. "
                    "Create a production application key set in the eBay Developer Program."
                )
            if self._on_call is not None:
                self._on_call()
            credentials = (
                f"{self._settings.ebay_client_id}:{self._settings.ebay_client_secret}"
            ).encode("utf-8")
            basic = base64.b64encode(credentials).decode("ascii")
            try:
                response = self._http.post(
                    self.token_url,
                    data={
                        "grant_type": "client_credentials",
                        "scope": SCOPE,
                    },
                    headers={
                        "Authorization": f"Basic {basic}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
            except httpx.HTTPError as exc:
                raise EbayAuthError(f"OAuth request failed: {exc}") from exc

            if response.status_code != 200:
                detail = _safe_error_body(response)
                raise EbayAuthError(
                    f"OAuth token request returned HTTP {response.status_code}: {detail}"
                )
            try:
                payload = response.json()
                access_token = payload["access_token"]
                expires_in = float(payload.get("expires_in", 7200))
            except (ValueError, KeyError, TypeError) as exc:
                raise EbayAuthError("OAuth response was malformed") from exc

            # refresh a little early so a slow request cannot use an expired token
            self._token = Token(
                access_token=str(access_token),
                expires_at=self._clock() + max(60.0, expires_in - 120.0),
                token_type=str(payload.get("token_type", "Bearer")),
            )
            return self._token.access_token


def _safe_error_body(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(payload, dict):
        error = payload.get("error") or payload.get("error_description")
        if error:
            return str(error)[:300]
    return str(payload)[:300]
