"""Euro conversion using ECB daily reference rates.

All target marketplaces (IT, DE, AT and later FR/ES) quote in EUR, so a missing
rate never blocks the core use case. Rates are cached in SQLite and refreshed
at most once per day. Non-EUR values are rejected rather than guessed when no
rate is available.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Callable, Mapping

import httpx

from src.storage.repository import Repository

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


class FxUnavailable(RuntimeError):
    """No trustworthy conversion rate is available."""


class FxService:
    def __init__(
        self,
        repository: Repository,
        *,
        http: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
        max_stale_days: int = 7,
    ) -> None:
        self._repo = repository
        self._http = http or httpx.Client(timeout=10.0, follow_redirects=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_stale_days = max_stale_days
        self._rates: dict[str, Decimal] = {"EUR": Decimal("1")}
        self._rates_day: str | None = None

    def close(self) -> None:
        self._http.close()

    def convert_minor(self, amount_minor: int, currency: str) -> int:
        rate = self.rate_per_eur(currency)
        if rate is None:
            raise FxUnavailable(f"no {currency}/EUR rate available")
        value = (Decimal(amount_minor) / rate).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        return int(value)

    def convert_optional(self, amount_minor: int | None, currency: str | None) -> int | None:
        if amount_minor is None or not currency:
            return None
        return self.convert_minor(amount_minor, currency)

    def rate_per_eur(self, currency: str) -> Decimal | None:
        code = (currency or "").upper()
        if code == "EUR":
            return Decimal("1")
        self._ensure_rates()
        return self._rates.get(code)

    def _ensure_rates(self) -> None:
        today = self._clock().astimezone(timezone.utc).strftime("%Y-%m-%d")
        if self._rates_day == today and len(self._rates) > 1:
            return
        try:
            response = self._http.get(ECB_URL)
            if response.status_code != 200:
                raise FxUnavailable(f"ECB returned HTTP {response.status_code}")
            rates = parse_ecb_rates(response.text)
            if rates:
                self._rates = {"EUR": Decimal("1"), **rates}
                self._rates_day = today
                self._repo.save_fx_rates(
                    today, {code: str(rate) for code, rate in rates.items()}
                )
                return
        except (httpx.HTTPError, ET.ParseError, FxUnavailable, ValueError):
            pass

        cached = self._repo.latest_fx_rates()
        if cached:
            self._rates = {"EUR": Decimal("1")}
            for code, raw in cached.items():
                try:
                    self._rates[code] = Decimal(raw)
                except InvalidOperation:
                    continue
            self._rates_day = today
        # Stale rates are still better than none for the same day; the caller
        # decides whether an estimate is good enough to alert on.

    def rates(self) -> Mapping[str, Decimal]:
        self._ensure_rates()
        return dict(self._rates)


def parse_ecb_rates(xml_text: str) -> dict[str, Decimal]:
    root = ET.fromstring(xml_text)
    rates: dict[str, Decimal] = {}
    for element in root.iter():
        if not element.tag.endswith("Cube"):
            continue
        currency = element.attrib.get("currency")
        rate = element.attrib.get("rate")
        if not currency or not rate:
            continue
        try:
            parsed = Decimal(rate)
        except InvalidOperation:
            continue
        if parsed > 0:
            rates[currency.upper()] = parsed
    return rates
