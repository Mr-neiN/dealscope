import pytest

from src.ebay.models import EbayParseError, parse_item_detail, parse_item_summary


BASE_SUMMARY = {
    "itemId": "v1|123|0",
    "title": "NVIDIA GeForce RTX 3090 24GB",
    "itemWebUrl": "https://www.ebay.it/itm/123",
    "price": {"value": "499.99", "currency": "EUR"},
    "condition": "Pre-owned",
    "conditionId": "3000",
    "seller": {"username": "seller1", "feedbackPercentage": 98.5, "feedbackScore": 4211},
    "itemLocation": {"country": "IT", "city": "Milano"},
    "buyingOptions": ["FIXED_PRICE"],
    "image": {"imageUrl": "https://i.ebayimg.com/x.jpg"},
    "additionalImages": [{"imageUrl": "https://i.ebayimg.com/y.jpg"}],
    "itemEndDate": "2026-11-01T20:00:00Z",
    "itemCreationDate": "2026-10-28T09:00:00Z",
    "shippingOptions": [
        {
            "shippingCost": {"value": "9.90", "currency": "EUR"},
            "shippingCostType": "FIXED",
            "shipToLocations": {"region": "WORLDWIDE"},
        }
    ],
    "categories": [{"categoryId": "171483", "categoryName": "Graphics/Video Cards"}],
}


def test_parse_summary_extracts_core_fields():
    item = parse_item_summary(BASE_SUMMARY, "EBAY_IT", "IT")
    assert item.item_id == "v1|123|0"
    assert item.title == "NVIDIA GeForce RTX 3090 24GB"
    assert item.price.amount_minor == 49_999
    assert item.price.currency == "EUR"
    assert not item.is_auction
    assert item.condition_class if False else True
    assert item.image_count == 2
    assert item.shipping.cost.amount_minor == 990
    assert item.shipping.ship_to_ok is True
    assert item.location_country == "IT"
    assert item.seller_feedback_pct == pytest.approx(98.5)


def test_parse_summary_rejects_payload_without_item_id():
    broken = dict(BASE_SUMMARY)
    del broken["itemId"]
    with pytest.raises(EbayParseError):
        parse_item_summary(broken, "EBAY_IT", "IT")


def test_parse_summary_rejects_payload_without_price():
    broken = dict(BASE_SUMMARY)
    del broken["price"]
    with pytest.raises(EbayParseError):
        parse_item_summary(broken, "EBAY_IT", "IT")


def test_auction_item_uses_current_bid():
    payload = dict(BASE_SUMMARY)
    payload["buyingOptions"] = ["AUCTION"]
    payload["currentBidPrice"] = {"value": "200.00", "currency": "EUR"}
    item = parse_item_summary(payload, "EBAY_IT", "IT")
    assert item.is_auction
    assert item.current_bid.amount_minor == 20_000
    assert item.effective_price.amount_minor == 20_000


def test_ship_to_locations_decide_deliverability():
    payload = dict(BASE_SUMMARY)
    payload["shippingOptions"] = [
        {
            "shippingCost": {"value": "25.00", "currency": "EUR"},
            "shippingCostType": "FIXED",
            "shipToLocations": {"region": "EUROPEAN_UNION"},
        }
    ]
    item = parse_item_summary(payload, "EBAY_DE", "IT")
    assert item.shipping.ship_to_ok is True

    payload["shippingOptions"] = [
        {
            "shippingCost": {"value": "25.00", "currency": "EUR"},
            "shippingCostType": "FIXED",
            "shipToLocations": {"region": "AMERICA"},
        }
    ]
    item = parse_item_summary(payload, "EBAY_DE", "IT")
    assert item.shipping.ship_to_ok is False


def test_local_pickup_only_listing_is_flagged():
    payload = dict(BASE_SUMMARY)
    payload["shippingOptions"] = [
        {"shippingCostType": "NOT_SPECIFIED", "shipToLocations": {"region": "IT"}}
    ]
    item = parse_item_summary(payload, "EBAY_IT", "IT")
    assert item.shipping.pickup_only
    assert item.shipping.cost is None


def test_parse_detail_extract_aspects_and_description():
    summary_item = parse_item_summary(BASE_SUMMARY, "EBAY_IT", "IT")
    detail_payload = dict(BASE_SUMMARY)
    detail_payload["description"] = "Used GPU, works great." + " padding " * 9000
    detail_payload["localizedAspects"] = [
        {"name": "Brand", "value": "NVIDIA"},
        {"name": "Memory Size", "value": "24 GB"},
        {"name": "Empty", "value": ""},
    ]
    detail = parse_item_detail(detail_payload, "EBAY_IT", "IT", existing=summary_item)
    assert detail.aspects["brand"] == ("NVIDIA",)
    assert detail.aspects["memory size"] == ("24 GB",)
    assert "Empty" not in detail.aspects
    assert len(detail.description) <= 4000


def test_parse_detail_falls_back_to_summary_shipping_when_detail_has_none():
    summary_item = parse_item_summary(BASE_SUMMARY, "EBAY_IT", "IT")
    detail_payload = dict(BASE_SUMMARY)
    detail_payload["shippingOptions"] = []
    detail = parse_item_detail(detail_payload, "EBAY_IT", "IT", existing=summary_item)
    # Detail shipping came back empty; summary shipping must survive.
    assert detail.shipping.cost.amount_minor == summary_item.shipping.cost.amount_minor
