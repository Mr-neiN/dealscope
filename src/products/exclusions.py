"""Listing classification and risk signals.

The output separates the listing *kind* (complete product, bundle, accessory,
defect, box only, ambiguous) from the operational *risk level*. A defective
listing is never given a working-product valuation by the pricing layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.config import ExclusionRules
from src.products.matcher import MatchResult
from src.products.parser import (
    ParsedTitle,
    condition_flags,
    contains_any,
    contains_phrase,
    normalize_phrase,
)

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Bundle words that are meaningful on their own. Multiplier tokens such as 2x
# only imply a bundle when several distinct products were identified.
STRONG_BUNDLE_TERMS = (
    "bundle",
    "lot",
    "job lot",
    "konvolut",
    "sammlung",
    "collection",
    "combo",
    "set",
    "kit",
    "lotto",
    "pacco",
    "mehrere",
    "multiple",
)


@dataclass(frozen=True)
class RiskAssessment:
    kind: str
    risk_level: str
    flags: tuple[str, ...]
    reasons: tuple[str, ...]
    is_bundle: bool = False
    is_untested: bool = False
    is_defect: bool = False
    is_accessory: bool = False

    @property
    def blocked(self) -> bool:
        """Kinds that must never receive a normal working-product alert."""
        return self.kind in ("defect", "box_only", "accessory") or self.risk_level == "critical"


class ListingClassifier:
    def __init__(
        self,
        rules: ExclusionRules,
        *,
        low_seller_feedback_pct: float = 95.0,
    ) -> None:
        self.rules = rules
        self.low_seller_feedback_pct = low_seller_feedback_pct
        self._defect = [normalize_phrase(term) for term in rules.defect_terms]
        self._untested = [normalize_phrase(term) for term in rules.untested_terms]
        self._box_only = [normalize_phrase(term) for term in rules.box_only_terms]
        self._accessory = [normalize_phrase(term) for term in rules.accessory_terms]
        self._accessory_only = [
            normalize_phrase(term) for term in rules.accessory_only_terms
        ]
        self._overrides = [
            normalize_phrase(term) for term in rules.accessory_override_phrases
        ]
        self._bundle = [normalize_phrase(term) for term in rules.bundle_terms]
        self._strong_bundle = [normalize_phrase(term) for term in STRONG_BUNDLE_TERMS]
        self._ambiguous = [normalize_phrase(term) for term in rules.ambiguous_terms]
        self._suspicious = [normalize_phrase(term) for term in rules.suspicious_terms]

    def classify(
        self,
        *,
        parsed: ParsedTitle,
        match: MatchResult,
        condition_label: str | None,
        condition_class: str,
        shipping_known: bool,
        ship_to_ok: bool | None,
        seller_feedback_pct: float | None,
        seller_feedback_score: int | None,
        detail_text: str | None = None,
    ) -> RiskAssessment:
        text = parsed.normalized
        flags: list[str] = []
        reasons: list[str] = []
        risk = "low"

        def raise_risk(level: str) -> None:
            nonlocal risk
            if RISK_ORDER[level] > RISK_ORDER[risk]:
                risk = level

        detail_normalized = normalize_phrase(detail_text or "")
        detail_box_hits = contains_any(detail_normalized, self._box_only)
        box_hits = contains_any(text, self._box_only)
        if box_hits or detail_box_hits:
            flags.extend(f"box_only:{hit}" for hit in (*box_hits, *detail_box_hits))
            return RiskAssessment(
                kind="box_only",
                risk_level="critical",
                flags=tuple(flags),
                reasons=("listing appears to be packaging or excludes the item",),
            )

        defect_hits = contains_any(text, self._defect)
        detail_defect_hits = contains_any(detail_normalized, self._defect)
        if condition_class == "for_parts":
            flags.append("for_parts_condition")
        if defect_hits or detail_defect_hits:
            flags.extend(f"defect:{hit}" for hit in (*defect_hits, *detail_defect_hits))
            if detail_defect_hits:
                flags.append("defect_in_description")
        if defect_hits or detail_defect_hits or condition_class == "for_parts":
            return RiskAssessment(
                kind="defect",
                risk_level="critical",
                flags=tuple(flags),
                reasons=("defective or parts-only listing is not comparable to working stock",),
                is_defect=True,
            )

        accessory_only_hits = contains_any(text, self._accessory_only)
        detail_accessory_hits = contains_any(detail_normalized, self._accessory_only)
        if accessory_only_hits or detail_accessory_hits:
            flags.extend(
                f"accessory_only:{hit}"
                for hit in (*accessory_only_hits, *detail_accessory_hits)
            )
            if detail_accessory_hits:
                flags.append("accessory_in_description")
            return RiskAssessment(
                kind="accessory",
                risk_level="critical",
                flags=tuple(flags),
                reasons=("explicit accessory-only listing",),
                is_accessory=True,
            )

        accessory_hits = contains_any(text, self._accessory)
        override_hits = contains_any(text, self._overrides)
        if accessory_hits:
            if override_hits or match.primary is not None:
                flags.extend(f"accessory_mentioned:{hit}" for hit in accessory_hits)
                reasons.append("accessory wording present but a complete product may be offered")
                raise_risk("medium")
            else:
                flags.extend(f"accessory:{hit}" for hit in accessory_hits)
                return RiskAssessment(
                    kind="accessory",
                    risk_level="critical",
                    flags=tuple(flags),
                    reasons=("accessory wording without product identity",),
                    is_accessory=True,
                )

        if match.ambiguous:
            flags.append("ambiguous_model")
            reasons.append("title matches more than one catalogue product equally well")
            raise_risk("high")

        untested_hits = contains_any(text, self._untested)
        if untested_hits:
            flags.extend(f"untested:{hit}" for hit in untested_hits)
            reasons.append("functionality is not confirmed")
            raise_risk("high")

        ambiguous_hits = contains_any(text, self._ambiguous)
        if ambiguous_hits:
            flags.extend(f"vague:{hit}" for hit in ambiguous_hits)
            reasons.append("title refers to the description or photos for key details")
            raise_risk("high")

        suspicious_hits = contains_any(text, self._suspicious)
        if suspicious_hits:
            flags.extend(f"suspicious:{hit}" for hit in suspicious_hits)
            reasons.append("wording may indicate a replica or misleading item")
            raise_risk("high")

        condition_extra = condition_flags(condition_label)
        flags.extend(condition_extra)
        if condition_class == "unknown":
            flags.append("condition_unknown")
            reasons.append("condition could not be classified")
            raise_risk("medium")

        bundle_hits = contains_any(text, self._bundle)
        strong_bundle_hits = contains_any(text, self._strong_bundle)
        multiple_components = len(match.components) >= 2
        is_bundle = multiple_components or bool(strong_bundle_hits)
        if is_bundle:
            flags.append("bundle")
            reasons.append("mixed lot or multi-item listing")
            if bundle_hits:
                flags.extend(f"bundle_term:{hit}" for hit in bundle_hits)
            raise_risk("medium")

        if not shipping_known:
            flags.append("shipping_unknown")
            reasons.append("delivery cost is not available")
            raise_risk("medium")
        if ship_to_ok is False:
            flags.append("not_deliverable")
            reasons.append("listing does not appear to ship to the destination country")
            raise_risk("high")
        if ship_to_ok is None:
            flags.append("deliverability_unconfirmed")
            reasons.append("delivery to the destination country is unconfirmed")
            raise_risk("medium")

        if seller_feedback_pct is not None:
            if seller_feedback_pct < self.low_seller_feedback_pct:
                flags.append(f"low_seller_feedback:{seller_feedback_pct:.1f}")
                reasons.append("seller feedback is below the configured threshold")
                raise_risk("medium")
            if seller_feedback_score is not None and seller_feedback_score < 10:
                flags.append("low_seller_volume")
                raise_risk("medium")

        kind = "bundle" if is_bundle else "complete"
        return RiskAssessment(
            kind=kind,
            risk_level=risk,
            flags=tuple(flags),
            reasons=tuple(reasons),
            is_bundle=is_bundle,
            is_untested=bool(untested_hits),
        )
