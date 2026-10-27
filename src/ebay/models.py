"""Typed representations of the parts of eBay responses the scanner uses.

Only the fields needed for discovery, risk assessment and valuation are
retained. Raw API payloads are not stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from src.money import MoneyError, parse_optional_minor

EU_COUNTRIES = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DK",
        "EE",
        "FI",
        "FR",
        "DE",
        "GR",
        "HU",
        "IE",
        "IT",
        "LV",
        "LT",
        "LU",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SK",
        "SI",
        "ES",
        "SE",
    }
)


class EbayParseError(ValueError):
    """An API response could not be interpreted safely."""


@dataclass(frozen=True)
class Money:
    amount_minor: int
    currency: str

    def __str__(self) -> str:
        return f"{self.amount_minor} {self.currency}"


@dataclass(frozen=True)
class ShippingQuote:
    cost: Money | None
    import_charges: Money | None
    cost_type: str | None
    ship_to_ok: bool | None
    ship_to_locations: tuple[str, ...] = ()
    pickup_only: bool = False


@dataclass(frozen=True)
class ListingItem:
    item_id: str
    marketplace: str
    title: str
    url: str
    price: Money
    current_bid: Money | None
    bid_count: int | None
    buying_options: tuple[str, ...]
    condition_label: str | None
    condition_id: str | None
    item_end_at: datetime | None
    item_creation_date: datetime | None
    seller_name: str | None
    seller_feedback_pct: float | None
    seller_feedback_score: int | None
    location_country: str | None
    location_city: str | None
    shipping: ShippingQuote
    image_count: int
    category_ids: tuple[str, ...] = ()
    description: str | None = None
    aspects: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    quantity_available: int | None = None
    availability: str | None = None

    @property
    def is_auction(self) -> bool:
        return "AUCTION" in self.buying_options

    @property
    def effective_price(self) -> Money:
        if self.is_auction and self.current_bid is not None:
            return self.current_bid
        return self.price


@dataclass(frozen=True)
class SearchPage:
    items: tuple[ListingItem, ...]
    total: int
    offset: int
    limit: int
    warnings: tuple[str, ...] = ()


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _money(payload: object) -> Money | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("value")
    currency = payload.get("currency")
    if value is None or not isinstance(currency, str) or not currency:
        return None
    try:
        parsed = parse_optional_minor(value)
    except MoneyError:
        return None
    if parsed is None:
        return None
    return Money(parsed, currency.upper())


def _shipping_from_summary(
    options: object, delivery_country: str, item_ship_to: object | None = None
) -> ShippingQuote:
    parsed_options: list[dict[str, Any]] = []
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, Mapping):
                continue
            cost = _money(option.get("shippingCost"))
            import_charges = _money(option.get("importCharges"))
            locations_payload = option.get("shipToLocations")
            locations: list[str] = []
            if isinstance(locations_payload, Mapping):
                region = locations_payload.get("region")
                country = locations_payload.get("country")
                if isinstance(region, str) and region:
                    locations.append(region.upper())
                if isinstance(country, str) and country:
                    locations.append(country.upper())
            elif isinstance(locations_payload, list):
                locations.extend(
                    str(item).upper() for item in locations_payload if isinstance(item, str)
                )
            parsed_options.append(
                {
                    "cost": cost,
                    "import": import_charges,
                    "cost_type": option.get("shippingCostType"),
                    "locations": tuple(locations),
                    "pickup_only": bool(option.get("localPickup"))
                    or str(option.get("shippingCostType", "")).upper() == "NOT_SPECIFIED",
                }
            )

    item_locations: tuple[str, ...] = ()
    if isinstance(item_ship_to, list):
        item_locations = tuple(
            str(entry).upper() for entry in item_ship_to if isinstance(entry, str)
        )

    def ships_here(locations: Sequence[str]) -> bool | None:
        effective = list(locations) or list(item_locations)
        if not effective:
            return None
        for location in effective:
            if location in ("WORLDWIDE", delivery_country):
                return True
            if location in ("EUROPEAN_UNION", "CONTINENTAL_EUROPE") and delivery_country in EU_COUNTRIES:
                return True
        return False

    if not parsed_options:
        return ShippingQuote(
            cost=None,
            import_charges=None,
            cost_type=None,
            ship_to_ok=True if item_locations and ships_here(item_locations) else None,
            ship_to_locations=item_locations,
            pickup_only=True,
        )

    evaluated = [
        (option, ships_here(option["locations"])) for option in parsed_options
    ]
    deliverable = [pair for pair in evaluated if pair[1] is True]
    pool = deliverable or evaluated

    def sort_key(pair: tuple[dict[str, Any], bool | None]) -> tuple[int, int, int]:
        option = pair[0]
        cost = option["cost"]
        return (
            0 if pair[1] is True else 1,
            0 if cost is not None else 1,
            cost.amount_minor if cost is not None else 10**12,
        )

    chosen, chosen_ok = sorted(pool, key=sort_key)[0]
    if deliverable:
        ship_to = True
    elif all(option["locations"] or item_locations for option, _ in evaluated):
        ship_to = False
    else:
        ship_to = None

    return ShippingQuote(
        cost=chosen["cost"],
        import_charges=chosen["import"],
        cost_type=chosen["cost_type"],
        ship_to_ok=ship_to,
        ship_to_locations=chosen["locations"] or item_locations,
        pickup_only=bool(chosen["pickup_only"]),
    )


def _aspects(payload: object) -> Mapping[str, tuple[str, ...]]:
    aspects: dict[str, tuple[str, ...]] = {}
    if not isinstance(payload, list):
        return aspects
    for aspect in payload:
        if not isinstance(aspect, Mapping):
            continue
        name = aspect.get("name") or aspect.get("localizedAspectName")
        values = aspect.get("value") or aspect.get("localizedAspectValues")
        if not isinstance(name, str) or not name:
            continue
        if isinstance(values, list):
            cleaned = tuple(str(value) for value in values if str(value).strip())
        elif isinstance(values, str):
            cleaned = (values,)
        else:
            cleaned = ()
        if cleaned:
            aspects[name.lower()] = cleaned
    return aspects


def parse_item_summary(
    payload: object, marketplace: str, delivery_country: str
) -> ListingItem:
    if not isinstance(payload, Mapping):
        raise EbayParseError("item summary is not an object")
    item_id = payload.get("itemId")
    title = payload.get("title")
    if not isinstance(item_id, str) or not item_id:
        raise EbayParseError("item summary has no itemId")
    if not isinstance(title, str) or not title.strip():
        raise EbayParseError(f"item {item_id} has no title")

    price = _money(payload.get("price"))
    current_bid = _money(payload.get("currentBidPrice"))
    if price is None:
        price = current_bid
    if price is None:
        raise EbayParseError(f"item {item_id} has no usable price")

    seller = payload.get("seller") if isinstance(payload.get("seller"), Mapping) else {}
    feedback_pct = seller.get("feedbackPercentage")
    try:
        feedback_pct_value = float(feedback_pct) if feedback_pct is not None else None
    except (TypeError, ValueError):
        feedback_pct_value = None
    feedback_score = seller.get("feedbackScore")
    try:
        feedback_score_value = int(feedback_score) if feedback_score is not None else None
    except (TypeError, ValueError):
        feedback_score_value = None

    location = payload.get("itemLocation") if isinstance(payload.get("itemLocation"), Mapping) else {}
    shipping = _shipping_from_summary(
        payload.get("shippingOptions"), delivery_country, payload.get("shipToLocations")
    )

    images = 0
    if isinstance(payload.get("image"), Mapping) and payload["image"].get("imageUrl"):
        images += 1
    if isinstance(payload.get("additionalImages"), list):
        images += len(
            [entry for entry in payload["additionalImages"] if isinstance(entry, Mapping)]
        )
    if isinstance(payload.get("thumbnailImages"), list) and images == 0:
        images = len(payload["thumbnailImages"])

    buying_options_payload = payload.get("buyingOptions")
    buying_options = (
        tuple(str(option) for option in buying_options_payload if isinstance(option, str))
        if isinstance(buying_options_payload, list)
        else ()
    )
    categories_payload = payload.get("categories")
    category_ids: list[str] = []
    if isinstance(categories_payload, list):
        for category in categories_payload:
            if isinstance(category, Mapping) and category.get("categoryId"):
                category_ids.append(str(category["categoryId"]))

    bid_count_raw = payload.get("bidCount")
    try:
        bid_count = int(bid_count_raw) if bid_count_raw is not None else None
    except (TypeError, ValueError):
        bid_count = None

    url = payload.get("itemWebUrl") or payload.get("itemAffiliateWebUrl")
    if not isinstance(url, str) or not url.startswith("http"):
        url = f"https://www.ebay.com/itm/{item_id}"

    return ListingItem(
        item_id=item_id,
        marketplace=marketplace,
        title=title.strip(),
        url=url,
        price=price,
        current_bid=current_bid,
        bid_count=bid_count,
        buying_options=buying_options,
        condition_label=(
            str(payload["condition"]) if isinstance(payload.get("condition"), str) else None
        ),
        condition_id=(
            str(payload["conditionId"]) if payload.get("conditionId") is not None else None
        ),
        item_end_at=parse_timestamp(payload.get("itemEndDate")),
        item_creation_date=parse_timestamp(payload.get("itemCreationDate")),
        seller_name=str(seller.get("username")) if seller.get("username") else None,
        seller_feedback_pct=feedback_pct_value,
        seller_feedback_score=feedback_score_value,
        location_country=(
            str(location.get("country")).upper() if location.get("country") else None
        ),
        location_city=str(location.get("city")) if location.get("city") else None,
        shipping=shipping,
        image_count=images,
        category_ids=tuple(category_ids),
    )


def parse_item_detail(
    payload: object,
    marketplace: str,
    delivery_country: str,
    existing: ListingItem | None = None,
) -> ListingItem:
    base = existing or parse_item_summary(payload, marketplace, delivery_country)
    if not isinstance(payload, Mapping):
        raise EbayParseError("item detail is not an object")

    description_raw = payload.get("description")
    description = (
        str(description_raw)[:4000] if isinstance(description_raw, str) and description_raw else None
    )
    aspects = _aspects(payload.get("localizedAspects"))
    shipping = _shipping_from_summary(
        payload.get("shippingOptions") or [], delivery_country, payload.get("shipToLocations")
    )
    if shipping.cost is None and base.shipping.cost is not None and shipping.ship_to_ok is None:
        shipping = replace(
            shipping,
            cost=base.shipping.cost,
            import_charges=base.shipping.import_charges,
            ship_to_ok=base.shipping.ship_to_ok,
            ship_to_locations=base.shipping.ship_to_locations,
        )

    availability = None
    availabilities = payload.get("estimatedAvailabilities")
    if isinstance(availabilities, list) and availabilities:
        first = availabilities[0]
        if isinstance(first, Mapping):
            availability = str(
                first.get("estimatedAvailabilityStatus")
                or first.get("availabilityThresholdType")
                or ""
            ) or None

    quantity = payload.get("availableQuantity")
    try:
        quantity_available = int(quantity) if quantity is not None else base.quantity_available
    except (TypeError, ValueError):
        quantity_available = base.quantity_available

    url = payload.get("itemWebUrl") or base.url
    return replace(
        base,
        url=url if isinstance(url, str) and url else base.url,
        description=description,
        aspects=aspects,
        shipping=shipping,
        quantity_available=quantity_available,
        availability=availability,
    )
