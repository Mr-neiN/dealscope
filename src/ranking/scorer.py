"""Deal ranking, confidence and risk gating.

A large discount cannot compensate for a critical risk flag or weak evidence.
The output keeps `deal_score`, `confidence_score` and `risk_level` separate so
the notification can state all three honestly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from src.config import AppConfig, CategorySpec, ProductSpec
from src.ebay.models import ListingItem  # noqa: F401  (typing aid for callers)
from src.pricing.cost import CostBreakdown
from src.pricing.estimator import Valuation
from src.products.exclusions import RiskAssessment
from src.storage.repository import ListingRecord

DISCOUNT_WEIGHT = 0.40
SAVINGS_WEIGHT = 0.20
EVIDENCE_WEIGHT = 0.25
COMPLETENESS_WEIGHT = 0.15

HIGH_RISK_DISCOUNT_MARGIN = 10.0
EXTREME_DISCOUNT_PCT = 60.0


@dataclass(frozen=True)
class Evaluation:
    alertable: bool
    alert_kind: str | None
    suppress_reasons: tuple[str, ...]
    risk_level: str
    risk_flags: tuple[str, ...]
    deal_score: float
    confidence_score: float
    confidence_label: str
    discount_pct: float
    savings_eur_minor: int
    value_eur_minor: int | None
    total_cost_eur_minor: int | None
    max_bid_eur_minor: int | None
    completeness: float
    urgency: float
    valuation: Valuation | None
    cost: CostBreakdown | None
    components: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class DealScorer:
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def evaluate(
        self,
        *,
        listing: ListingRecord,
        product: ProductSpec | None,
        match_confidence: float,
        risk: RiskAssessment,
        valuation: Valuation | None,
        cost: CostBreakdown | None,
        category: CategorySpec,
        now: datetime,
        components: Sequence[str] = (),
        detail_fetched: bool = False,
    ) -> Evaluation:
        flags = tuple(dict.fromkeys(risk.flags))
        reasons: list[str] = []
        notes: list[str] = []
        bundle = risk.is_bundle or listing.listing_kind == "bundle"

        if risk.blocked:
            reasons.append(f"listing kind/risk is disqualifying ({risk.kind}, {risk.risk_level})")
        if valuation is None:
            reasons.append("no trustworthy valuation for this exact product and condition")
        if cost is None:
            reasons.append("total acquisition cost could not be computed")
        if listing.price_eur_minor is None:
            reasons.append("price could not be converted to EUR")
        if product is None and not bundle:
            reasons.append("product identity is not confirmed")
        if risk.flags and "not_deliverable" in risk.flags:
            reasons.append("seller does not ship to the destination country")
        if valuation is not None and valuation.provisional:
            if not self._config.alerts.allow_provisional:
                reasons.append("valuation is provisional (active asking prices or unverified reference)")

        if valuation is None or cost is None or listing.price_eur_minor is None:
            return Evaluation(
                alertable=False,
                alert_kind=None,
                suppress_reasons=tuple(reasons),
                risk_level=risk.risk_level,
                risk_flags=flags,
                deal_score=0.0,
                confidence_score=0.0,
                confidence_label="provisional",
                discount_pct=0.0,
                savings_eur_minor=0,
                value_eur_minor=valuation.value_eur_minor if valuation else None,
                total_cost_eur_minor=cost.total_eur_minor if cost else None,
                max_bid_eur_minor=None,
                completeness=self._completeness(listing),
                urgency=0.0,
                valuation=valuation,
                cost=cost,
                components=tuple(components),
                notes=tuple(notes),
            )

        value = valuation.value_eur_minor
        total_cost = cost.total_eur_minor
        savings = value - total_cost
        discount = (savings / value * 100.0) if value > 0 else 0.0
        overall_confidence = valuation.confidence_score * 0.7 + max(
            0.0, min(match_confidence, 1.0)
        ) * 0.3
        confidence_label = _confidence_label(overall_confidence)
        completeness = self._completeness(listing)
        evidence_component = min(overall_confidence, 1.0)
        discount_component = min(
            max(discount, 0.0) / max(self._config.ranking.full_discount_score_pct, 1.0),
            1.0,
        )
        savings_component = min(
            max(savings, 0) / max(category.full_savings_score_eur * 100, 1),
            1.0,
        )
        deal_score = 100.0 * (
            DISCOUNT_WEIGHT * discount_component
            + SAVINGS_WEIGHT * savings_component
            + EVIDENCE_WEIGHT * evidence_component
            + COMPLETENESS_WEIGHT * completeness
        )
        if risk.blocked:
            deal_score = 0.0

        max_bid = self._max_bid(value, cost) if listing.buying_option == "auction" else None
        urgency = self._urgency(listing, now) if listing.buying_option == "auction" else 0.0

        if discount < 0:
            reasons.append("total cost exceeds the estimated fair value")
        if discount > EXTREME_DISCOUNT_PCT:
            flags = flags + ("extreme_discount",)
            notes.append(
                "discount is unusually large; verify condition, seller and shipping before buying"
            )
            if not detail_fetched:
                reasons.append("unusually large discount has not been verified against item details")

        min_discount = (
            self._config.ranking.bundle_min_discount_pct
            if bundle
            else category.min_discount_pct
        )
        min_savings = (
            self._config.ranking.bundle_min_savings_eur * 100
            if bundle
            else category.min_savings_eur * 100
        )
        if discount < min_discount:
            reasons.append(
                f"discount {discount:.1f}% is below the {min_discount:.0f}% threshold"
            )
        if savings < min_savings:
            reasons.append(
                f"absolute saving {savings / 100:.0f} EUR is below the "
                f"{min_savings / 100:.0f} EUR threshold"
            )

        if risk.risk_level == "high":
            if discount < min_discount + HIGH_RISK_DISCOUNT_MARGIN:
                reasons.append("high-risk listing needs a larger discount")
            if savings < min_savings * 1.5:
                reasons.append("high-risk listing needs a larger absolute saving")
        elif risk.risk_level == "medium":
            if confidence_label in ("provisional", "low"):
                reasons.append("risk is elevated and the evidence is weak")

        required_confidence = self._config.alerts.min_confidence
        if not _confidence_at_least(confidence_label, required_confidence):
            reasons.append(
                f"confidence {confidence_label} is below the required {required_confidence}"
            )

        if valuation.provisional and self._config.alerts.allow_provisional:
            notes.append("provisional valuation: this is asking-price evidence, not sold data")

        alert_kind: str | None = None
        if listing.buying_option == "auction":
            if max_bid is None or listing.price_eur_minor > max_bid:
                reasons.append("current bid is above the rational maximum bid")
            minutes_left = self._minutes_left(listing, now)
            if minutes_left is None:
                reasons.append("auction end time is unknown")
            elif minutes_left <= 0:
                reasons.append("auction has ended")
            elif minutes_left > self._config.alerts.auction_alert_hours * 60:
                reasons.append("auction is not ending within the alert window")
            alert_kind = "auction_ending"
        elif bundle:
            alert_kind = "bundle_deal"
        else:
            alert_kind = "deal"

        alertable = not reasons
        if not alertable:
            alert_kind = None

        return Evaluation(
            alertable=alertable,
            alert_kind=alert_kind,
            suppress_reasons=tuple(dict.fromkeys(reasons)),
            risk_level=risk.risk_level,
            risk_flags=flags,
            deal_score=round(deal_score, 1),
            confidence_score=round(overall_confidence, 3),
            confidence_label=confidence_label,
            discount_pct=round(discount, 1),
            savings_eur_minor=savings,
            value_eur_minor=value,
            total_cost_eur_minor=total_cost,
            max_bid_eur_minor=max_bid,
            completeness=round(completeness, 3),
            urgency=urgency,
            valuation=valuation,
            cost=cost,
            components=tuple(components),
            notes=tuple(notes),
        )

    def _max_bid(self, value_eur_minor: int, cost: CostBreakdown) -> int:
        margin = self._config.ranking.auction_safety_margin_pct
        bid_budget = int(value_eur_minor * (100 - margin) / 100)
        # Shipping, import and fees are unavoidable and reduce the bid ceiling.
        return max(0, bid_budget - cost.non_price_costs_minor)

    def _urgency(self, listing: ListingRecord, now: datetime) -> float:
        minutes = self._minutes_left(listing, now)
        if minutes is None or minutes <= 0:
            return 0.0
        if minutes <= 6 * 60:
            return 1.0
        if minutes <= 24 * 60:
            return 0.75
        if minutes <= self._config.alerts.auction_alert_hours * 60:
            return 0.5
        return 0.0

    @staticmethod
    def _minutes_left(listing: ListingRecord, now: datetime) -> float | None:
        if not listing.item_end_at:
            return None
        try:
            end = datetime.fromisoformat(listing.item_end_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return (end - now).total_seconds() / 60.0

    def _completeness(self, listing: ListingRecord) -> float:
        score = 0.0
        if listing.product_id:
            score += 0.2
        if listing.image_count >= 3:
            score += 0.15
        elif listing.image_count >= 1:
            score += 0.08
        if listing.condition_class != "unknown":
            score += 0.15
        if listing.shipping_known:
            score += 0.2
        if listing.seller_feedback_pct is not None:
            score += 0.1
        if listing.detail_fetched_at:
            score += 0.2
        return min(score, 1.0)


def _confidence_label(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= 0.6:
        return "medium"
    if score >= 0.4:
        return "low"
    return "provisional"


def _confidence_at_least(actual: str, required: str) -> bool:
    order = {"provisional": 0, "low": 1, "medium": 2, "high": 3}
    return order.get(actual, 0) >= order.get(required, 2)
