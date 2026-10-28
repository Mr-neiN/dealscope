from pathlib import Path

import pytest

from src.config import load_config
from src.pricing.cost import compute_cost

from tests.test_matcher import ENV, PROJECT_ROOT


@pytest.fixture(scope="module")
def config():
    return load_config(ENV, base_dir=PROJECT_ROOT)


def test_known_shipping_and_import_are_used_as_given(config):
    cost = compute_cost(
        price_eur_minor=30_000,
        shipping_eur_minor=1_500,
        import_charges_eur_minor=200,
        seller_country="AT",
        delivery_country="IT",
        category=config.categories["gpu"],
        alerts=config.alerts,
    )
    assert cost.total_eur_minor == 30_000 + 1_500 + 200
    assert cost.shipping_known
    assert not cost.import_estimated
    assert cost.non_price_costs_minor == 1_700


def test_unknown_shipping_falls_back_to_category_estimate(config):
    cost = compute_cost(
        price_eur_minor=30_000,
        shipping_eur_minor=None,
        import_charges_eur_minor=None,
        seller_country="IT",
        delivery_country="IT",
        category=config.categories["gpu"],
        alerts=config.alerts,
    )
    assert not cost.shipping_known
    expected = max(
        config.categories["gpu"].default_shipping_eur,
        config.alerts.unknown_shipping_estimate_eur,
    ) * 100
    assert cost.shipping_eur_minor == expected
    assert any("shipping unknown" in note for note in cost.notes)


def test_non_eu_seller_gets_import_vat_estimate(config):
    cost = compute_cost(
        price_eur_minor=20_000,
        shipping_eur_minor=2_000,
        import_charges_eur_minor=None,
        seller_country="CH",
        delivery_country="IT",
        category=config.categories["gpu"],
        alerts=config.alerts,
    )
    assert cost.import_estimated
    assert cost.import_charges_eur_minor == int((20_000 + 2_000) * 0.22 + 0.5)
    assert any("import VAT" in note for note in cost.notes)


def test_eu_seller_never_gets_import_charges_added(config):
    cost = compute_cost(
        price_eur_minor=20_000,
        shipping_eur_minor=2_000,
        import_charges_eur_minor=None,
        seller_country="DE",
        delivery_country="IT",
        category=config.categories["gpu"],
        alerts=config.alerts,
    )
    assert cost.import_charges_eur_minor == 0
    assert not cost.import_estimated
    assert cost.total_eur_minor == 22_000


def test_negative_inputs_are_clamped(config):
    cost = compute_cost(
        price_eur_minor=1_000,
        shipping_eur_minor=-5,
        import_charges_eur_minor=-3,
        seller_country="IT",
        delivery_country="IT",
        category=config.categories["ram"],
        alerts=config.alerts,
    )
    assert cost.shipping_eur_minor == 0
    assert cost.import_charges_eur_minor == 0
    assert cost.total_eur_minor == 1_000
