"""Gather comparable evidence for a product and condition.

Evidence quality is ranked explicitly. Active listings are evidence of what
sellers ask, never of what buyers paid, and are kept in their own tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.config import AppConfig, ReferencePrice
from src.storage.repository import Repository


@dataclass(frozen=True)
class PricePoint:
    price_eur_minor: int
    observed_at: datetime
    weight: int = 1
    label: str = ""


@dataclass(frozen=True)
class ComparableEvidence:
    sold: tuple[PricePoint, ...]
    reference: ReferencePrice | None
    active: tuple[PricePoint, ...]
    sold_window_days: int
    active_window_days: int


class ComparableService:
    def __init__(
        self,
        repository: Repository,
        config: AppConfig,
        *,
        sold_window_days: int = 90,
        active_window_days: int = 14,
    ) -> None:
        self._repo = repository
        self._config = config
        self._sold_window_days = sold_window_days
        self._active_window_days = active_window_days
        self._references = {
            (reference.product_id, reference.condition_class): reference
            for reference in config.references
        }

    def collect(
        self,
        *,
        product_id: str,
        condition_class: str,
        item_id: str,
        now: datetime,
    ) -> ComparableEvidence:
        sold_since = now - timedelta(days=self._sold_window_days)
        sold_rows = self._repo.get_comparables(product_id, condition_class, sold_since)
        sold: list[PricePoint] = []
        for row in sold_rows:
            if not row.get("verified"):
                continue
            observed = _parse_iso(row.get("observed_at"))
            if observed is None:
                continue
            try:
                price = int(row["price_eur_minor"])
                weight = max(1, int(row.get("sample_size") or 1))
            except (KeyError, TypeError, ValueError):
                continue
            if price <= 0:
                continue
            sold.append(
                PricePoint(
                    price_eur_minor=price,
                    observed_at=observed,
                    weight=weight,
                    label=str(row.get("source_ref") or "verified sale"),
                )
            )

        active_since = now - timedelta(days=self._active_window_days)
        active_rows = self._repo.get_active_listing_prices(
            product_id, condition_class, active_since, item_id
        )
        active: list[PricePoint] = []
        for row in active_rows:
            observed = _parse_iso(row.get("last_seen_at"))
            price = row.get("price_eur_minor")
            if observed is None or price is None:
                continue
            try:
                amount = int(price)
            except (TypeError, ValueError):
                continue
            if amount > 0:
                active.append(
                    PricePoint(
                        price_eur_minor=amount,
                        observed_at=observed,
                        weight=1,
                        label="active listing",
                    )
                )

        return ComparableEvidence(
            sold=tuple(sold),
            reference=self._references.get((product_id, condition_class)),
            active=tuple(active),
            sold_window_days=self._sold_window_days,
            active_window_days=self._active_window_days,
        )


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
