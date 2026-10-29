"""Feed publisher tests: serialization, filtering and push behavior."""

from __future__ import annotations

import httpx
import pytest

from src.config import ConfigError
from src.publishing.feed import (
    PublishError,
    collect_candidates,
    feed_deal_from_listing,
    publish_deals,
)

from tests.test_matcher import ENV, PROJECT_ROOT


class StubStore:
    """Captures the dashboard filters the publisher uses."""

    def __init__(self, items: list[dict[str, object]]):
        self.items = items
        self.calls: list[dict[str, object]] = []

    def listings(self, filters: dict[str, object]) -> dict[str, object]:
        self.calls.append(filters)
        return {"total": len(self.items), "limit": filters["limit"], "items": self.items}


def test_feed_deal_serializes_dashboard_row():
    row = {
        "item_id": "itm-1",
        "marketplace": "EBAY_DE",
        "product_id": "rtx_3090",
        "product_name": "RTX 3090",
        "title": "RTX 3090 used, working",
        "url": "https://www.ebay.de/itm/itm-1",
        "price_eur_minor": 30_000,
        "total_cost_eur_minor": 31_500,
        "value_eur_minor": 48_000,
        "discount_pct": 34.4,
        "deal_score": 91.5,
        "valuation_label": "verified",
        "condition_class": "used_working",
        "condition_label": "Used",
        "product_category": "gpu",
        "item_end_at": None,
        "last_seen_at": "2026-10-29T10:00:00Z",
        "ship_to_ok": 1,
        "shipping_known": 1,
    }
    deal = feed_deal_from_listing(row)
    assert deal["item_id"] == "itm-1"
    assert deal["marketplace"] == "EBAY_DE"
    assert deal["product_label"] == "RTX 3090"
    assert deal["price_eur"] == 30_000
    assert deal["total_eur"] == 31_500
    assert deal["fair_value_eur"] == 48_000
    assert deal["discount_pct"] == 34
    assert deal["deal_score"] == 92
    assert deal["confidence"] == "verified"
    assert deal["search_ok"] is True
    assert deal["shipping_ok"] is True
    assert deal["ends_at"] == ""
    assert len(deal["title"]) == len("RTX 3090 used, working")


def test_collect_candidates_uses_dashboard_filters_and_maps_rows():
    items = [
        {
            "item_id": "itm-1",
            "marketplace": "EBAY_IT",
            "title": "deal",
            "url": "https://www.ebay.it/itm/1",
            "price_eur_minor": 20_000,
            "total_cost_eur_minor": 20_000,
            "value_eur_minor": 40_000,
            "deal_score": 80.0,
            "discount_pct": 50.0,
        },
        {
            "item_id": "itm-2",
            "marketplace": "EBAY_AT",
            "title": "deal 2",
            "url": "https://www.ebay.at/itm/2",
            "price_eur_minor": 10_000,
            "total_cost_eur_minor": 10_000,
            "value_eur_minor": 20_000,
            "deal_score": 70.0,
            "discount_pct": 50.0,
        },
    ]
    store = StubStore(items)
    deals = collect_candidates(store, limit=10)
    filters = store.calls[0]
    assert len(deals) == 2
    assert filters["deals_only"] == "1"
    assert filters["status"] == "active"
    assert set(str(filters["condition"]).split(",")) == {"new", "used_working", "refurbished"}
    assert set(str(filters["risk"]).split(",")) == {"low", "medium"}
    assert deals[0]["item_id"] == "itm-1"


def test_collect_candidates_caps_limit():
    store = StubStore([])
    collect_candidates(store, limit=500)
    assert store.calls[0]["limit"] == 100


class MemoryTransport(httpx.BaseTransport):
    """Queued canned responses for the publish call."""

    def __init__(self, responses: list[httpx.Response]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def test_publish_deals_success_sends_token_and_batch():
    transport = MemoryTransport(
        [httpx.Response(200, json={"ok": True, "pushed": 1, "pushed_at": "2026-10-29T00:00:00Z"})]
    )
    result = publish_deals(
        [
            {
                "item_id": "itm-1",
                "marketplace": "EBAY_IT",
                "title": "deal",
                "url": "https://www.ebay.it/itm/1",
                "price_eur": 20_000,
                "total_eur": 20_000,
                "deal_score": 80,
            }
        ],
        site_url="https://feed.example.com/",
        token="secret-token",
        transport=transport,
    )
    assert result.pushed == 1
    assert result.site_url == "https://feed.example.com/"
    request = transport.requests[0]
    assert request.url == "https://feed.example.com/api/push"
    assert request.headers["authorization"] == "Bearer secret-token"


def test_publish_deals_rejection_raises_publish_error():
    transport = MemoryTransport([httpx.Response(422, text="invalid deal payload")])
    with pytest.raises(PublishError) as error:
        publish_deals(
            [{"item_id": "itm-1"}],
            site_url="https://feed.example.com",
            token="t",
            transport=transport,
        )
    assert "422" in str(error.value)


def test_publish_deals_http_error_raises_publish_error():
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(failing)
    with pytest.raises(PublishError, match="ConnectError"):
        publish_deals([], site_url="https://feed.example.com", token="t", transport=transport)


def test_feed_settings_are_optional_but_paired():
    from src.config import load_config

    paired_env = dict(ENV, FEED_SITE_URL="https://feed.example.com", FEED_PUBLISH_TOKEN="tok")
    config = load_config(paired_env, base_dir=PROJECT_ROOT)
    assert config.settings.feed_site_url == "https://feed.example.com"
    assert config.settings.feed_publish_token == "tok"

    # Only one of the two keys is an error (empty token neutralizes dotenv).
    broken = dict(ENV, FEED_SITE_URL="https://feed.example.com", FEED_PUBLISH_TOKEN="")
    with pytest.raises(ConfigError):
        load_config(broken, base_dir=PROJECT_ROOT)
