"""Push current deal candidates to the hosted DealScope feed site.

The public feed is read-only; this module is the only writer. It reuses the
dashboard's listing query so the feed shows exactly what the operator sees
after confirmation filters (active, complete enough, non-critical risk),
serializes each candidate into the compact JSON the feed worker validates,
and posts them with the publish token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import httpx

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_TITLE_LENGTH = 200


class PublishError(RuntimeError):
    """The feed site rejected or could not receive a push."""


@dataclass(frozen=True)
class PublishResult:
    site_url: str
    pushed: int


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    return int(round(float(value)))  # type: ignore[arg-type]


def _as_bool_or_none(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes"):
            return True
        if lowered in ("0", "false", "no"):
            return False
        return None
    return bool(value)


def feed_deal_from_listing(row: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a dashboard listing row into the compact feed-payload shape."""
    product_id = str(row.get("product_id") or "")
    product_label = str(row.get("product_name") or "") or product_id or "Unknown"
    price_eur = _as_int(row.get("price_eur_minor"))
    return {
        "item_id": str(row.get("item_id") or ""),
        "marketplace": str(row.get("marketplace") or ""),
        "product_id": product_id,
        "product_label": product_label[:80],
        "title": str(row.get("title") or "")[:MAX_TITLE_LENGTH],
        "url": str(row.get("url") or ""),
        "thumbnail_url": "",
        "condition_label": str(row.get("condition_label") or row.get("condition_class") or "unknown"),
        "category": str(row.get("product_category") or ""),
        "price_eur": price_eur,
        "total_eur": _as_int(row.get("total_cost_eur_minor")) or price_eur or 0,
        "fair_value_eur": _as_int(row.get("value_eur_minor")),
        "discount_pct": _as_int(row.get("discount_pct")),
        "deal_score": _as_int(row.get("deal_score")) or 0,
        "confidence": str(row.get("valuation_label") or "estimated")[:20],
        "ends_at": str(row.get("item_end_at") or ""),
        "observed_at": str(row.get("last_seen_at") or ""),
        "search_ok": _as_bool_or_none(row.get("ship_to_ok")),
        "shipping_ok": bool(row.get("shipping_known") or 0),
        "import_estimated": None,
    }


def collect_candidates(
    store: Any,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Top active deal candidates, using the dashboard's own filters.

    Only listings the operator would act on are published: active, complete
    enough to be valued, best-value condition classes, and never high risk.
    """
    filters = {
        "deals_only": "1",
        "status": "active",
        "condition": "new,used_working,refurbished",
        "risk": "low,medium",
        "sort": "deal",
        "limit": min(max(limit, 0), 100),
    }
    payload = store.listings(filters)
    rows: Sequence[Mapping[str, Any]] = payload.get("items", []) if isinstance(payload, dict) else []
    return [feed_deal_from_listing(row) for row in rows]


def publish_deals(
    deals: Sequence[Mapping[str, Any]],
    *,
    site_url: str,
    token: str,
    timeout: float = 15.0,
    transport: httpx.BaseTransport | None = None,
) -> PublishResult:
    """POST one batch of deals to the feed site; raises :class:`PublishError`."""
    endpoint = site_url.rstrip("/") + "/api/push"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.post(
                endpoint,
                json={"deals": [dict(deal) for deal in deals]},
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise PublishError(
            f"feed request failed: {type(exc).__name__}: {exc}"
        ) from exc
    if response.status_code != 200:
        raise PublishError(
            f"feed rejected the push (HTTP {response.status_code}): "
            f"{response.text[:200]}"
        )
    try:
        pushed = int(response.json().get("pushed", len(deals)))
    except (ValueError, TypeError):
        pushed = len(deals)
    return PublishResult(site_url=site_url, pushed=pushed)
