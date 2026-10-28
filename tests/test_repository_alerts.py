from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from src.alerts.service import AlertService
from src.alerts.telegram import TelegramError, TelegramNotifier, format_alert
from src.config import load_config
from src.ranking.scorer import Evaluation
from src.storage.database import Database
from src.storage.repository import Repository

from tests.test_matcher import ENV, PROJECT_ROOT
from tests.test_ranking import make_listing

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def config():
    return load_config(ENV, base_dir=PROJECT_ROOT)


@pytest.fixture
def repo(tmp_path):
    database = Database(tmp_path / "alerts.db")
    database.initialize()
    repository = Repository(database)
    yield repository
    repository.close()


class FakeRepo:
    def __init__(self):
        self.calls = {}

    def calls_today(self, day, endpoint=None):
        if endpoint is None:
            return sum(self.calls.get((day, ep, mp), 0) for (d, ep, mp) in list(self.calls) if d == day)
        return self.calls.get((day, endpoint, ""), 0)

    def record_api_call(self, day, endpoint, marketplace=""):
        key = (day, endpoint, marketplace)
        self.calls[key] = self.calls.get(key, 0) + 1


def test_notifier_redacts_token_from_network_errors(repo):
    class ExplodingClient(httpx.Client):
        def post(self, *args, **kwargs):
            raise httpx.ConnectError(
                "failed to connect to https://api.telegram.org/bot123:SECRET/sendMessage"
            )

    notifier = TelegramNotifier(
        token="123:SECRET",
        chat_id="42",
        http=ExplodingClient(timeout=1),
    )
    with pytest.raises(TelegramError) as excinfo:
        notifier.send("hello")
    message = str(excinfo.value)
    assert "123:SECRET" not in message
    assert "***REDACTED***" in message
    notifier.close()


def test_format_alert_contains_key_facts():
    payload = {
        "title": "NVIDIA GeForce RTX 3090",
        "price_eur_minor": 40_000,
        "shipping_eur_minor": 1_500,
        "shipping_known": True,
        "import_charges_eur_minor": 0,
        "total_cost_eur_minor": 41_500,
        "value_eur_minor": 62_000,
        "discount_pct": 32.9,
        "savings_eur_minor": 20_500,
        "confidence_label": "high",
        "confidence_score": 0.85,
        "source_kind": "sold_verified",
        "source": "verified sales",
        "sample_size": 8,
        "window_days": 30,
        "risk_level": "low",
        "risk_flags": [],
        "notes": [],
        "marketplace": "EBAY_IT",
        "condition": "Pre-owned",
        "url": "https://www.ebay.it/itm/123",
        "is_auction": False,
        "first_seen_at": "2026-10-28T10:00:00Z",
    }
    message = format_alert(payload)
    assert "RTX 3090" in message
    assert "€400" in message
    assert "high" in message.casefold()
    assert "href=\"https://www.ebay.it/itm/123\"" in message
    # Secret-looking strings in titles must not break HTML parsing.
    html_safe = format_alert({**payload, "title": "<script>alert(1)</script>"})
    assert "<script>" not in html_safe


def make_listing_item(item_id="itm-alert-1", marketplace="EBAY_IT", title="NVIDIA GeForce RTX 3090"):
    from src.ebay.models import ListingItem, Money, ShippingQuote

    return ListingItem(
        item_id=item_id,
        marketplace=marketplace,
        title=title,
        url=f"https://www.ebay.it/itm/{item_id}",
        price=Money(40_000, "EUR"),
        current_bid=None,
        bid_count=None,
        buying_options=("FIXED_PRICE",),
        condition_label="Pre-owned",
        condition_id="3000",
        item_end_at=None,
        item_creation_date=None,
        seller_name="seller",
        seller_feedback_pct=99.0,
        seller_feedback_score=500,
        location_country="IT",
        location_city=None,
        shipping=ShippingQuote(cost=Money(1_500, "EUR"), import_charges=None, cost_type="FIXED", ship_to_ok=True),
        image_count=2,
    )


def ingest_listing(repo, item_id="itm-alert-1"):
    from src.storage.repository import IngestedListing

    ingested = IngestedListing(
        item=make_listing_item(item_id=item_id),
        normalized_title="nvidia geforce rtx 3090",
        condition_class="used_working",
        buying_option="fixed",
        price_eur_minor=40_000,
        shipping_eur_minor=1_500,
        import_charges_eur_minor=None,
        current_bid_eur_minor=None,
    )
    repo.ingest_listing(ingested, NOW)


def make_evaluation(config, *, alertable=True, kind="deal", discount=40.0, savings=20_000):
    from src.products.exclusions import RiskAssessment

    return Evaluation(
        alertable=alertable,
        alert_kind=kind if alertable else None,
        suppress_reasons=() if alertable else ("suppressed",),
        risk_level="low",
        risk_flags=(),
        deal_score=80.0,
        confidence_score=0.85,
        confidence_label="high",
        discount_pct=discount,
        savings_eur_minor=savings,
        value_eur_minor=62_000,
        total_cost_eur_minor=41_500,
        max_bid_eur_minor=None,
        completeness=1.0,
        urgency=1.0,
        valuation=None,
        cost=None,
        components=(),
        notes=(),
    )


def test_live_alert_is_created_pending_then_delivered(repo, config):
    notifier = TelegramNotifier(
        token="t", chat_id="c", http=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})
            )
        )
    )
    service = AlertService(repo, config, notifier, shadow_mode=False)
    ingest_listing(repo)
    listing = make_listing(item_id="itm-alert-1")
    created = service.maybe_create(
        evaluation=make_evaluation(config),
        listing=listing,
        payload={"title": "RTX 3090", "price_eur_minor": 40_000, "risk_level": "low", "risk_flags": []},
        now=NOW,
    )
    assert created
    pending = repo.pending_alerts_due(NOW, limit=10)
    assert len(pending) == 1
    stats = service.deliver_pending(NOW)
    assert stats.delivered == 1
    history = repo.alerts_for_listing(listing.item_id)
    assert history[0].state == "delivered"
    notifier.close()


def test_duplicate_price_event_is_deduplicated(repo, config):
    notifier = TelegramNotifier(
        token="t", chat_id="c", http=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            )
        )
    )
    service = AlertService(repo, config, notifier, shadow_mode=False)
    ingest_listing(repo)
    listing = make_listing(item_id="itm-alert-1")
    payload = {"title": "RTX 3090", "price_eur_minor": 40_000, "risk_level": "low", "risk_flags": []}
    assert service.maybe_create(evaluation=make_evaluation(config), listing=listing, payload=payload, now=NOW)
    stats = service.deliver_pending(NOW)
    assert stats.delivered == 1
    again = service.maybe_create(
        evaluation=make_evaluation(config), listing=listing, payload=payload, now=NOW
    )
    assert not again
    notifier.close()


def test_shadow_alerts_never_enter_pending_delivery(repo, config):
    service = AlertService(repo, config, notifier=None, shadow_mode=True)
    ingest_listing(repo)
    listing = make_listing(item_id="itm-alert-1")
    created = service.maybe_create(
        evaluation=make_evaluation(config),
        listing=listing,
        payload={"title": "RTX 3090", "price_eur_minor": 40_000, "risk_level": "low", "risk_flags": []},
        now=NOW,
    )
    assert created
    assert service.deliver_pending(NOW).delivered == 0
    history = repo.alerts_for_listing(listing.item_id)
    assert history[0].state == "shadow"
