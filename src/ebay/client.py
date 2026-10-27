"""eBay Browse and Taxonomy HTTP client.

Only documented production endpoints are used. Retries are bounded, respect
Retry-After, and every attempt is charged to the daily quota ledger.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping

import httpx

from src.config import Settings
from src.ebay.auth import EbayAuth, EbayAuthError
from src.ebay.models import (
    EbayParseError,
    ListingItem,
    SearchPage,
    parse_item_summary,
)
from src.ebay.quota import QuotaLedger


class EbayError(RuntimeError):
    """Base class for eBay failures."""


class EbayRequestError(EbayError):
    def __init__(self, status_code: int, message: str, *, path: str = "") -> None:
        self.status_code = status_code
        self.path = path
        super().__init__(f"eBay HTTP {status_code}: {message}")


class EbayRateLimited(EbayError):
    def __init__(self, retry_after: float | None, message: str) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class EbayResponseError(EbayError):
    """The response was received but could not be understood."""


SORTS = {"newlyListed", "endingSoonest", "price", "-price"}


class EbayClient:
    def __init__(
        self,
        settings: Settings,
        auth: EbayAuth,
        quota: QuotaLedger,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self._settings = settings
        self._auth = auth
        self._quota = quota
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            follow_redirects=True,
        )
        self._sleep = sleep
        self._random = random_source or random.Random()
        self._max_attempts = max_attempts or settings.http_max_attempts
        self._base_url = (
            "https://api.sandbox.ebay.com"
            if settings.ebay_environment == "sandbox"
            else "https://api.ebay.com"
        )

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ Browse
    def search(
        self,
        *,
        marketplace: str,
        query: str,
        sort: str = "newlyListed",
        limit: int = 50,
        offset: int = 0,
        category_ids: str | None = None,
        buying_options: str | None = None,
        condition_ids: str | None = None,
        extra_filters: Mapping[str, str] | None = None,
    ) -> SearchPage:
        if sort not in SORTS:
            raise ValueError(f"unsupported sort: {sort}")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if offset < 0 or offset > 9999:
            raise ValueError("offset must be between 0 and 9999")

        filters: dict[str, str] = {}
        if self._settings.ship_to_country:
            filters["deliveryCountry"] = self._settings.ship_to_country
        if category_ids:
            filters["categoryIds"] = category_ids
        if buying_options:
            filters["buyingOptions"] = buying_options
        if condition_ids:
            filters["conditionIds"] = condition_ids
        if extra_filters:
            filters.update(extra_filters)

        params: dict[str, Any] = {
            "q": query,
            "limit": str(limit),
            "offset": str(offset),
            "sort": sort,
        }
        if filters:
            params["filter"] = ",".join(
                f"{key}:{value}" for key, value in filters.items()
            )

        payload = self._get_json(
            "/buy/browse/v1/item_summary/search",
            marketplace=marketplace,
            endpoint="browse_search",
            params=params,
        )
        warnings_payload = payload.get("warnings")
        warnings: list[str] = []
        if isinstance(warnings_payload, list):
            for warning in warnings_payload:
                if isinstance(warning, Mapping):
                    message = warning.get("message") or warning.get("longMessage")
                    if message:
                        warnings.append(str(message))
        summaries = payload.get("itemSummaries")
        if summaries is None:
            summaries = []
        if not isinstance(summaries, list):
            raise EbayResponseError("itemSummaries was not a list")

        items: list[ListingItem] = []
        for summary in summaries:
            try:
                items.append(
                    parse_item_summary(
                        summary,
                        marketplace,
                        self._settings.ship_to_country,
                    )
                )
            except EbayParseError:
                # One malformed item must not discard the rest of a page.
                continue
        try:
            total = int(payload.get("total", len(items)))
        except (TypeError, ValueError):
            total = len(items)
        return SearchPage(items=tuple(items), total=total, offset=offset, limit=limit, warnings=tuple(warnings))

    def get_item(self, item_id: str, marketplace: str) -> Mapping[str, Any]:
        if not item_id:
            raise ValueError("item_id is required")
        return self._get_json(
            f"/buy/browse/v1/item/{item_id}",
            marketplace=marketplace,
            endpoint="browse_item",
            params={},
        )

    # ---------------------------------------------------------------- Taxonomy
    def taxonomy_tree_id(self, marketplace: str) -> str:
        payload = self._get_json(
            "/commerce/taxonomy/v1/get_default_category_tree_id",
            marketplace=marketplace,
            endpoint="taxonomy_tree",
            params={"marketplace_id": marketplace},
        )
        tree_id = payload.get("categoryTreeId")
        if not tree_id:
            raise EbayResponseError("taxonomy response has no categoryTreeId")
        return str(tree_id)

    def taxonomy_suggestions(self, tree_id: str, query: str) -> list[dict[str, Any]]:
        payload = self._get_json(
            f"/commerce/taxonomy/v1/category_tree/{tree_id}/get_category_suggestions",
            marketplace="",
            endpoint="taxonomy_suggestions",
            params={"q": query},
        )
        suggestions = payload.get("categorySuggestions")
        if not isinstance(suggestions, list):
            return []
        return [entry for entry in suggestions if isinstance(entry, Mapping)]

    # ------------------------------------------------------------------ HTTP
    def _get_json(
        self,
        path: str,
        *,
        marketplace: str,
        endpoint: str,
        params: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        attempts = max(1, self._max_attempts)
        force_refresh = False
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._sleep(self._backoff(attempt - 1, last_error))
            try:
                access_token = self._auth.get_token(force=force_refresh)
                force_refresh = False
            except EbayAuthError:
                raise

            self._quota.consume(endpoint, marketplace)
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            }
            if marketplace:
                headers["X-EBAY-C-MARKETPLACE-ID"] = marketplace
            if self._settings.ship_to_country and marketplace:
                context = f"contextualLocation=country={self._settings.ship_to_country}"
                if self._settings.delivery_postal_code:
                    context += f",zip={self._settings.delivery_postal_code}"
                headers["X-EBAY-C-ENDUSERCTX"] = context

            try:
                response = self._http.get(
                    f"{self._base_url}{path}",
                    params=dict(params),
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = EbayError(f"network error: {exc}")
                continue
            except httpx.HTTPError as exc:
                last_error = EbayError(f"HTTP client error: {exc}")
                continue

            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise EbayResponseError(f"malformed JSON from {path}") from exc
                if not isinstance(payload, Mapping):
                    raise EbayResponseError(f"unexpected JSON shape from {path}")
                return payload

            if response.status_code == 401:
                if attempt == 1:
                    force_refresh = True
                    last_error = EbayError("access token rejected (401)")
                    self._auth.invalidate()
                    continue
                raise EbayRequestError(401, _error_text(response), path=path)

            retry_after = _retry_after_seconds(response)
            if response.status_code == 429:
                # The next loop iteration sleeps according to this error, so
                # Retry-After is not applied twice.
                last_error = EbayRateLimited(
                    retry_after, f"rate limited by eBay for {path}"
                )
                continue

            if 500 <= response.status_code < 600:
                last_error = EbayRequestError(
                    response.status_code, _error_text(response), path=path
                )
                continue

            raise EbayRequestError(response.status_code, _error_text(response), path=path)

        if isinstance(last_error, EbayRateLimited):
            raise last_error
        raise EbayError(f"eBay request failed after {attempts} attempts: {last_error}")

    def _backoff(self, retry_number: int, last_error: Exception | None) -> float:
        if isinstance(last_error, EbayRateLimited) and last_error.retry_after:
            return min(last_error.retry_after, 60.0)
        cap = min(30.0, 1.0 * (2 ** (retry_number - 1)))
        return self._random.uniform(0.0, cap)

def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(payload, Mapping):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, Mapping):
                return str(
                    first.get("longMessage") or first.get("message") or first
                )[:300]
        for key in ("error_description", "message", "error"):
            if payload.get(key):
                return str(payload[key])[:300]
    return str(payload)[:300]
