from datetime import datetime, timezone
from pathlib import Path

import pytest
from dataclasses import replace as dataclass_replace

from src.config import load_config
from src.pricing.cost import compute_cost
from src.pricing.estimator import Valuation
from src.ranking.scorer import DealScorer
from src.storage.repository import ListingRecord

from tests.test_matcher import ENV, PROJECT_ROOT

NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def config():
    return load_config(ENV, base_dir=PROJECT_ROOT)


def make_listing(**overrides):
    fields = dict(
        item_id="itbl-1",
        marketplace="EBAY_IT",
        title="NVIDIA GeForce RTX 3090 24GB",
        normalized_title="nvidia geforce rtx 3090 24 gb",
        url="https://www.ebay.it/itm/1",
        price_minor=40_000,
        currency="EUR",
        price_eur_minor=40_000,
        shipping_eur_minor=1_500,
        shipping_known=True,
        ship_to_ok=True,
        import_charges_eur_minor=0,
        condition_label="Pre-owned",
        condition_class="used_working",
        buying_option="fixed",
        bid_count=None,
        current_bid_eur_minor=None,
        item_end_at=None,
        seller_name="seller",
        seller_feedback_pct=99.0,
        seller_feedback_score=500,
        location_country="IT",
        location_city=None,
        image_count=4,
        product_id=None,
        match_confidence=None,
        listing_kind="unknown",
        risk_level="medium",
        risk_flags=(),
        description_excerpt=None,
        aspects={},
        detail_fetched_at=None,
        status="active",
        first_seen_at="2026-10-28T10:00:00Z",
        last_seen_at="2026-10-28T11:00:00Z",
        last_alerted_at=None,
        total_cost_eur_minor=None,
        value_eur_minor=None,
        value_source_kind=None,
        value_source=None,
        valuation_label=None,
        valuation_score=None,
        valuation_sample_size=None,
        discount_pct=None,
        savings_eur_minor=None,
        max_bid_eur_minor=None,
        deal_score=None,
        confidence_score=None,
        completeness=None,
        evaluated_at=None,
    )
    fields.update(overrides)
    return ListingRecord(**fields)


SOLD_VALUATION = Valuation(
    value_eur_minor=62_000,
    source_kind="sold_verified",
    source="verified sales",
    sample_size=8,
    window_days=30,
    outliers_removed=0,
    confidence_score=0.9,
    confidence_label="high",
    method="recency-weighted median",
    provisional=False,
)


def evaluate(config, listing, risk, valuation=SOLD_VALUATION, product_id="rtx_3090",
             match_confidence=0.9, kind="complete"):
    scorer = DealScorer(config)
    category = config.categories["gpu"]
    cost = compute_cost(
        price_eur_minor=listing.price_eur_minor,
        shipping_eur_minor=listing.shipping_eur_minor,
        import_charges_eur_minor=0,
        seller_country="IT",
        delivery_country="IT",
        category=category,
        alerts=config.alerts,
    )
    product = config.product_by_id.get(product_id)
    return scorer.evaluate(
        listing=listing,
        product=product,
        match_confidence=match_confidence,
        risk=risk,
        valuation=valuation,
        cost=cost,
        category=category,
        now=NOW,
        components=(),
        detail_fetched=bool(listing.detail_fetched_at),
    )


@pytest.fixture
def risk_factory():
    from src.products.exclusions import RiskAssessment

    def make(**kwargs):
        base = dict(kind="complete", risk_level="low", flags=(), reasons=())
        base.update(kwargs)
        return RiskAssessment(**base)

    return make


def test_good_discount_alerts(config, risk_factory):
    listing = make_listing(detail_fetched_at="2026-10-28T11:30:00Z")
    risk = risk_factory()
    evaluation = evaluate(config, listing, risk)
    assert evaluation.alertable
    assert evaluation.alert_kind == "deal"
    assert evaluation.discount_pct > 30
    assert evaluation.deal_score > 0


def test_provisional_valuation_is_never_alertable_by_default(config, risk_factory):
    provisional = dataclass_replace(SOLD_VALUATION, provisional=True, confidence_label="provisional", confidence_score=0.25)
    evaluation = evaluate(config, make_listing(), risk_factory(), valuation=provisional)
    assert not evaluation.alertable
    assert any("provisional" in reason for reason in evaluation.suppress_reasons)


def test_low_confidence_is_suppressed(config, risk_factory):
    weak = dataclass_replace(SOLD_VALUATION, confidence_score=0.45, confidence_label="low")
    evaluation = evaluate(config, make_listing(detail_fetched_at="2026-10-28T11:30:00Z"), risk_factory(), valuation=weak, match_confidence=0.4)
    assert not evaluation.alertable
    assert any("confidence" in reason for reason in evaluation.suppress_reasons)


def test_extreme_discount_requires_detail_verification(config, risk_factory):
    listing = make_listing(price_minor=8_000, price_eur_minor=8_000)
    evaluation = evaluate(config, listing, risk_factory())
    assert not evaluation.alertable
    assert any("detail" in reason for reason in evaluation.suppress_reasons)
    assert "extreme_discount" in evaluation.risk_flags

    verified = make_listing(price_minor=8_000, price_eur_minor=8_000, detail_fetched_at="2026-10-28T11:30:00Z")
    verified_evaluation = evaluate(config, verified, risk_factory())
    assert verified_evaluation.alertable


def test_blocked_risk_never_alerts(config, risk_factory):
    risk = risk_factory(kind="defect", risk_level="critical")
    evaluation = evaluate(config, make_listing(), risk)
    assert not evaluation.alertable
    assert evaluation.deal_score == 0


def test_small_discount_is_below_threshold(config, risk_factory):
    listing = make_listing(detail_fetched_at="2026-10-28T11:30:00Z", price_minor=60_000, price_eur_minor=60_000)
    evaluation = evaluate(config, listing, risk_factory())
    assert not evaluation.alertable
    assert any("threshold" in reason for reason in evaluation.suppress_reasons)


def test_auction_gets_max_bid_urgency_and_alert_window(config, risk_factory):
    listing = make_listing(
        detail_fetched_at="2026-10-28T11:00:00Z",
        buying_option="auction",
        bid_count=3,
        current_bid_eur_minor=35_000,
        item_end_at="2026-10-29T10:00:00Z",
        price_minor=35_000,
        price_eur_minor=35_000,
    )
    evaluation = evaluate(config, listing, risk_factory())
    assert evaluation.alertable
    assert evaluation.alert_kind == "auction_ending"
    assert evaluation.max_bid_eur_minor <= SOLD_VALUATION.value_eur_minor * 0.8
    assert evaluation.urgency >= 0.5


def test_auction_ended_or_far_in_future_is_suppressed(config, risk_factory):
    past = make_listing(
        buying_option="auction",
        item_end_at="2026-10-27T10:00:00Z",
        price_minor=35_000,
        price_eur_minor=35_000,
    )
    evaluation = evaluate(config, past, risk_factory())
    assert not evaluation.alertable
    assert any("ended" in reason for reason in evaluation.suppress_reasons)

    future = make_listing(
        buying_option="auction",
        item_end_at="2026-11-20T10:00:00Z",
        price_minor=35_000,
        price_eur_minor=35_000,
    )
    evaluation = evaluate(config, future, risk_factory())
    assert not evaluation.alertable
    assert any("alert window" in reason for reason in evaluation.suppress_reasons)


def test_bundle_uses_stricter_thresholds(config, risk_factory):
    bundle_valuation = dataclass_replace(
        SOLD_VALUATION,
        source_kind="bundle_reference",
        method="component references with lot haircut",
    )
    listing = make_listing(
        listing_kind="bundle",
        risk_flags=("bundle",),
        detail_fetched_at="2026-10-28T11:30:00Z",
        price_minor=60_000,
        price_eur_minor=60_000,
    )
    risk = risk_factory(kind="bundle", is_bundle=True, risk_level="medium", flags=("bundle",))
    evaluation = evaluate(config, listing, risk, valuation=bundle_valuation)
    # A bundle must demand the stricter bundle discount threshold.
    if evaluation.discount_pct < config.ranking.bundle_min_discount_pct:
        assert not evaluation.alertable
        assert any("threshold" in reason for reason in evaluation.suppress_reasons)
    else:
        assert evaluation.alertable
