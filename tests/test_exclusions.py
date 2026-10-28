import pytest
from datetime import datetime

from src.products.exclusions import ListingClassifier, RiskAssessment
from src.products.matcher import MatchResult, ProductMatcher
from src.products.parser import parse_title

NOW = datetime(2026, 10, 28, 12, 0, 0)


@pytest.fixture(scope="module")
def classifier_and_matcher(config):
    classifier = ListingClassifier(
        config.exclusions,
        low_seller_feedback_pct=config.ranking.low_seller_feedback_pct,
    )
    matcher = ProductMatcher(config.products, config.variant_suffixes)
    return classifier, matcher


def classify(classifier_and_matcher, title, *, condition_class="used_working",
             condition_label="Used", shipping_known=True, ship_to_ok=True,
             seller_feedback_pct=99.0, seller_feedback_score=500, detail_text=None):
    classifier, matcher = classifier_and_matcher
    parsed = parse_title(title)
    match = matcher.match_parsed(parsed)
    if match.ambiguous:
        # Ambiguity is covered separately; neutralize it here for kind checks.
        match = match.__class__(
            parsed=parsed, primary=None, components=(),
            ambiguous=False, candidate_ids=(), notes=match.notes,
        )
    return classifier.classify(
        parsed=parsed,
        match=match,
        condition_label=condition_label,
        condition_class=condition_class,
        shipping_known=shipping_known,
        ship_to_ok=ship_to_ok,
        seller_feedback_pct=seller_feedback_pct,
        seller_feedback_score=seller_feedback_score,
        detail_text=detail_text,
    )


def test_clean_listing_is_complete_and_low_risk(classifier_and_matcher):
    risk = classify(classifier_and_matcher, "NVIDIA GeForce RTX 3090 24GB full")
    assert risk.kind == "complete"
    assert risk.risk_level == "low"
    assert not risk.blocked
    assert not risk.is_bundle and not risk.is_untested


def test_defect_title_is_blocked(classifier_and_matcher):
    risk = classify(classifier_and_matcher, "RTX 3090 scheda video DANNEGGIATA 24GB")
    assert risk.kind == "defect"
    assert risk.blocked


def test_for_parts_condition_is_blocked(classifier_and_matcher):
    risk = classify(
        classifier_and_matcher,
        "RTX 3090 24GB",
        condition_class="for_parts",
        condition_label="For parts or not working",
    )
    assert risk.kind == "defect"
    assert risk.blocked


def test_accessory_without_product_identity_is_blocked(classifier_and_matcher):
    risk = classify(classifier_and_matcher, "cavo alimentazione VGA originale")
    assert risk.kind == "accessory"
    assert risk.blocked


def test_untested_listing_is_flagged_high_risk(classifier_and_matcher):
    risk = classify(classifier_and_matcher, "RTX 3090 - scheda video non testata 24GB")
    assert risk.is_untested
    assert risk.risk_level == "high"
    assert not risk.blocked


def test_untested_hidden_only_in_description_is_detected(classifier_and_matcher):
    risk = classify(
        classifier_and_matcher,
        "NVIDIA RTX 3090 Founders Edition 24GB",
        detail_text="The card powers on but it is not tested, selling as is.",
    )
    assert risk.is_untested
    assert "untested_in_description" in risk.flags
    assert risk.risk_level == "high"


def test_box_only_listing_is_blocked_even_from_detail_text(classifier_and_matcher):
    risk = classify(
        classifier_and_matcher,
        "NVIDIA GeForce RTX 3090 24GB",
        detail_text="Box only, the graphics card is NOT included.",
    )
    assert risk.kind == "box_only"
    assert risk.blocked


def test_ambiguous_matches_raise_high_risk(classifier_and_matcher):
    # The current catalogue guards every ambiguous pair; the classifier must
    # still flag a hand-built ambiguous result defensively.
    classifier, matcher = classifier_and_matcher
    parsed = parse_title("ambiguous mystery card")
    ambiguous_match = MatchResult(
        parsed=parsed, primary=None, components=(), ambiguous=True,
        candidate_ids=(), notes=(),
    )
    risk = classifier.classify(
        parsed=parsed,
        match=ambiguous_match,
        condition_label="Used",
        condition_class="used_working",
        shipping_known=True,
        ship_to_ok=True,
        seller_feedback_pct=99.0,
        seller_feedback_score=500,
        detail_text=None,
    )
    assert "ambiguous_model" in risk.flags
    assert risk.risk_level == "high"


def test_unknown_shipping_and_unconfirmed_delivery_raise_risk(classifier_and_matcher):
    risk = classify(
        classifier_and_matcher,
        "NVIDIA GeForce RTX 3090 24GB working",
        shipping_known=False,
        ship_to_ok=None,
    )
    assert "shipping_unknown" in risk.flags
    assert risk.risk_level in ("medium", "high")


def test_not_deliverable_raises_high_risk(classifier_and_matcher):
    risk = classify(
        classifier_and_matcher,
        "NVIDIA GeForce RTX 3090 24GB working",
        ship_to_ok=False,
    )
    assert "not_deliverable" in risk.flags
    assert risk.risk_level == "high"


def test_low_seller_feedback_is_flagged(classifier_and_matcher):
    risk = classify(
        classifier_and_matcher,
        "NVIDIA GeForce RTX 3090 24GB working",
        seller_feedback_pct=90.0,
    )
    assert any(flag.startswith("low_seller_feedback") for flag in risk.flags)
    assert risk.risk_level in ("medium", "high")


def test_bundle_wording_is_detected_as_bundle(classifier_and_matcher):
    risk = classify(classifier_and_matcher, "Grafikkarten Konvolut 3 working")
    assert risk.is_bundle
    assert risk.kind == "bundle"
    assert risk.risk_level == "medium"


def test_exclusions_module_exposes_expected_api(classifier_and_matcher):
    classifier, _ = classifier_and_matcher
    assert classifier.rules is not None
    assert classifier.low_seller_feedback_pct > 0
