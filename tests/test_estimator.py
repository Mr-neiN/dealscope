import pytest

from src.config import load_config
from src.pricing.comparables import ComparableService, PricePoint
from src.pricing.estimator import PriceEstimator

from tests.test_matcher import ENV, PROJECT_ROOT
from src.products.matcher import ProductMatcher


NOW = __import__("datetime").datetime(2026, 10, 28, 12, 0, 0, tzinfo=__import__("datetime").timezone.utc)


@pytest.fixture(scope="module")
def config():
    return load_config(ENV, base_dir=PROJECT_ROOT)


class FakeRepo:
    def __init__(self, sold=(), active=()):
        self._sold = list(sold)
        self._active = list(active)

    def get_comparables(self, product_id, condition_class, since):
        return [row for row in self._sold if row["product_id"] == product_id]

    def get_active_listing_prices(self, product_id, condition_class, since, exclude_item_id, limit=30):
        return [row for row in self._active if row["product_id"] == product_id]


def sold_row(price, **kwargs):
    return {
        "product_id": "rtx_3090",
        "verified": kwargs.get("verified", True),
        "observed_at": kwargs.get("observed_at", "2026-10-20T00:00:00Z"),
        "price_eur_minor": price,
        "sample_size": kwargs.get("sample_size", 1),
        "source_ref": kwargs.get("source_ref", "verified sale"),
    }


def strong_sold_evidence(repo):
    for price in (60_000, 61_000, 62_000, 62_500, 63_000, 63_500, 64_000, 65_000):
        repo._sold.append(sold_row(price))


def test_sold_evidence_with_strong_sample_is_high_confidence(config):
    repo = FakeRepo()
    strong_sold_evidence(repo)
    estimator = PriceEstimator(ComparableService(repo, config), config)
    valuation = estimator.estimate(
        product=config.product_by_id["rtx_3090"],
        condition_class="used_working",
        item_id="itbl-1",
        now=NOW,
    )
    assert valuation is not None
    assert valuation.source_kind == "sold_verified"
    assert valuation.confidence_label == "high"
    assert not valuation.provisional
    assert valuation.value_eur_minor in range(62_000, 64_500)


def test_active_only_evidence_is_provisional_and_excluded_from_alerts(config):
    # "new" has no reference entry in the catalogue, so asking prices are all
    # there is for this condition.
    repo = FakeRepo(active=[
        {"product_id": "rtx_3090", "price_eur_minor": 60_000, "last_seen_at": "2026-10-25T00:00:00Z"},
        {"product_id": "rtx_3090", "price_eur_minor": 61_000, "last_seen_at": "2026-10-26T00:00:00Z"},
        {"product_id": "rtx_3090", "price_eur_minor": 62_000, "last_seen_at": "2026-10-27T00:00:00Z"},
    ])
    estimator = PriceEstimator(ComparableService(repo, config), config)
    valuation = estimator.estimate(
        product=config.product_by_id["rtx_3090"],
        condition_class="new",
        item_id="itbl-9",
        now=NOW,
    )
    assert valuation is not None
    assert valuation.source_kind == "active_listings"
    assert valuation.provisional
    assert not valuation.is_usable_for_normal_alert


def test_unverified_references_are_never_strong_evidence(config):
    repo = FakeRepo(sold=[sold_row(30_000, verified=False)], active=[
        {"product_id": "rtx_3090", "price_eur_minor": 60_000, "last_seen_at": "2026-10-25T00:00:00Z"},
        {"product_id": "rtx_3090", "price_eur_minor": 61_000, "last_seen_at": "2026-10-26T00:00:00Z"},
        {"product_id": "rtx_3090", "price_eur_minor": 62_000, "last_seen_at": "2026-10-27T00:00:00Z"},
    ])
    estimator = PriceEstimator(ComparableService(repo, config), config)
    valuation = estimator.estimate(
        product=config.product_by_id["rtx_3090"],
        condition_class="used_working",
        item_id="itbl-2",
        now=NOW,
    )
    assert valuation is not None
    assert valuation.source_kind != "sold_verified"


def test_verified_reference_beats_thin_sold_sample(config):
    repo = FakeRepo(
        sold=[sold_row(50_000), sold_row(52_000), sold_row(51_000)],
    )
    # Override references via config copy with a verified reference.
    from dataclasses import replace
    from src.config import ReferencePrice

    reference = ReferencePrice(
        product_id="rtx_3090",
        condition_class="used_working",
        price_eur_minor=62_000,
        sample_size=5,
        verified=True,
        source="manual verified 2026-10",
        observed_at=__import__("datetime").date(2026, 10, 1),
    )
    config_with_reference = replace(config, references=config.references + (reference,))
    estimator = PriceEstimator(ComparableService(repo, config_with_reference), config_with_reference)
    valuation = estimator.estimate(
        product=config_with_reference.product_by_id["rtx_3090"],
        condition_class="used_working",
        item_id="itbl-3",
        now=NOW,
    )
    assert valuation is not None
    assert valuation.source_kind == "reference"
    assert valuation.value_eur_minor == 62_000
    assert valuation.confidence_label == "medium"


def test_no_evidence_returns_none(config):
    repo = FakeRepo()
    estimator = PriceEstimator(ComparableService(repo, config), config)
    assert (
        estimator.estimate(
            product=config.product_by_id["rtx_3090"],
            condition_class="new",
            item_id="itbl-4",
            now=NOW,
        )
        is None
    )

    # With an unverified reference present the estimate exists but stays
    # provisional: placeholders can never drive a normal alert.
    unverified = estimator.estimate(
        product=config.product_by_id["rtx_3090"],
        condition_class="used_working",
        item_id="itbl-4b",
        now=NOW,
    )
    assert unverified is not None
    assert unverified.provisional
    assert unverified.confidence_label == "provisional"
    assert not unverified.is_usable_for_normal_alert


def test_untested_haircut_lowers_value_and_confidence(config):
    repo = FakeRepo()
    strong_sold_evidence(repo)
    estimator = PriceEstimator(ComparableService(repo, config), config)
    clean = estimator.estimate(
        product=config.product_by_id["rtx_3090"],
        condition_class="used_working",
        item_id="itbl-5",
        now=NOW,
    )
    untested = estimator.estimate(
        product=config.product_by_id["rtx_3090"],
        condition_class="used_working",
        item_id="itbl-5b",
        now=NOW,
        untested_haircut_pct=35.0,
    )
    assert clean is not None and untested is not None
    assert untested.value_eur_minor < clean.value_eur_minor
    assert untested.confidence_label == "low"
    assert any("functionality" in note or "reduced" in note for note in untested.notes)


def test_bundle_estimate_requires_two_verified_references(config):
    estimator = PriceEstimator(ComparableService(FakeRepo(), config), config)
    gpu = config.product_by_id["rtx_3090"]
    cpu = config.product_by_id["ryzen_5800x3d"]
    single = estimator.estimate_bundle(components=[gpu], condition_class="used_working", now=NOW)
    assert single is None

    from dataclasses import replace
    from src.config import ReferencePrice

    ref_gpu = ReferencePrice(
        product_id="rtx_3090", condition_class="used_working",
        price_eur_minor=60_000, sample_size=3, verified=True,
        source="manual", observed_at=__import__("datetime").date(2026, 9, 1),
    )
    ref_cpu = ReferencePrice(
        product_id="ryzen_5800x3d", condition_class="used_working",
        price_eur_minor=25_000, sample_size=3, verified=True,
        source="manual", observed_at=__import__("datetime").date(2026, 9, 1),
    )
    remaining = tuple(
        r for r in config.references if r.product_id not in {"rtx_3090", "ryzen_5800x3d"}
    )
    config_refs = replace(config, references=remaining + (ref_gpu, ref_cpu))
    estimator2 = PriceEstimator(ComparableService(FakeRepo(), config_refs), config_refs)
    bundle = estimator2.estimate_bundle(
        components=[gpu, cpu], condition_class="used_working", now=NOW
    )
    assert bundle is not None
    haircut = config.ranking.bundle_haircut_pct / 100
    expected = int(round((60_000 + 25_000) * (1 - haircut)))
    assert bundle.value_eur_minor == expected
    assert bundle.source_kind == "bundle_reference"
    assert bundle.provisional is False
