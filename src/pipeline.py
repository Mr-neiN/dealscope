"""End-to-end scan pipeline.

Responsibilities deliberately stay separate from the scheduler: this module
turns one search page into listings, observations, estimates and alerts, and
keeps expensive item-detail lookups for plausible candidates only.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from src.alerts.service import AlertService
from src.config import AppConfig, BundleSpec, CategorySpec, ProductSpec
from src.discovery.queries import SearchOutcome, SearchProfile
from src.ebay.client import EbayClient, EbayError
from src.ebay.models import (
    EbayParseError,
    ListingItem,
    parse_item_detail,
)
from src.ebay.quota import QuotaExceeded, QuotaLedger
from src.ebay.taxonomy import TaxonomyService
from src.pricing.cost import CostBreakdown, compute_cost
from src.pricing.estimator import PriceEstimator, Valuation
from src.pricing.fx import FxService, FxUnavailable
from src.products.exclusions import ListingClassifier, RiskAssessment
from src.products.matcher import MatchResult, ProductMatcher
from src.products.parser import condition_class_from_ebay, normalize_text
from src.ranking.scorer import DealScorer, Evaluation
from src.storage.repository import IngestedListing, ListingRecord, Repository, utc_iso

log = logging.getLogger(__name__)

PAGE_SIZE = 50
BUNDLE_CATEGORY = "bundle"


def _effective_match_confidence(match: MatchResult, risk: RiskAssessment) -> float:
    """Confidence actually usable for scoring.

    A bundle whose components were identified only from weak wording cannot
    claim the per-match confidence that a single exact model earns. The
    discount thresholds for bundles compensate by being stricter.
    """
    if not risk.is_bundle:
        return match.overall_confidence
    verified_count = sum(
        1
        for candidate in match.components
        if candidate.matched_phrase and candidate.confidence >= 0.6
    )
    if verified_count < 2:
        return min(match.overall_confidence, 0.45)
    return min(match.overall_confidence, 0.55 + 0.05 * (verified_count - 2))

class Scanner:
    def __init__(
        self,
        config: AppConfig,
        repository: Repository,
        client: EbayClient,
        matcher: ProductMatcher,
        classifier: ListingClassifier,
        estimator: PriceEstimator,
        scorer: DealScorer,
        alerts: AlertService,
        taxonomy: TaxonomyService,
        fx: FxService,
        quota: QuotaLedger,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._repo = repository
        self._client = client
        self._matcher = matcher
        self._classifier = classifier
        self._estimator = estimator
        self._scorer = scorer
        self._alerts = alerts
        self._taxonomy = taxonomy
        self._fx = fx
        self._quota = quota
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._bundles = {bundle.id: bundle for bundle in config.bundles}


    def run_search(self, profile: SearchProfile) -> SearchOutcome:
        now = self._clock()
        category_id: str | None = None
        if profile.use_category_filter and profile.taxonomy_hint:
            category_id = self._taxonomy.category_for(
                profile.marketplace, profile.taxonomy_hint
            )

        items_seen = 0
        new_items = 0
        pages = 0
        warnings: list[str] = []
        max_pages = max(1, profile.max_pages) if profile.strategy == "new_listings" else 1

        for page_index in range(max_pages):
            page = self._client.search(
                marketplace=profile.marketplace,
                query=profile.query,
                sort=profile.sort,
                limit=PAGE_SIZE,
                offset=page_index * PAGE_SIZE,
                category_ids=category_id,
            )
            pages += 1
            items_seen += len(page.items)
            warnings.extend(page.warnings)
            for item in page.items:
                try:
                    if self._process_item(item, profile, now):
                        new_items += 1
                except (EbayParseError, FxUnavailable, ValueError) as exc:
                    warnings.append(f"{item.item_id}: {exc}")
                    log.warning(
                        "listing skipped: %s",
                        exc,
                        extra={
                            "event": "listing_skipped",
                            "listing_id": item.item_id,
                            "job": profile.key,
                        },
                    )
                except Exception:
                    # One malformed listing must not abort an otherwise useful
                    # search page. The traceback is preserved for diagnosis.
                    log.exception(
                        "unexpected listing processing failure",
                        extra={"event": "listing_exception", "listing_id": item.item_id},
                    )

            if len(page.items) < PAGE_SIZE:
                break
            if page_index + 1 >= max_pages:
                break
            if self._quota.status().remaining <= self._config.budget.reserve_calls:
                warnings.append("pagination stopped to preserve the API reserve")
                break

        return SearchOutcome(
            status="ok",
            items_seen=items_seen,
            new_items=new_items,
            pages=pages,
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------------ ingest
    def _process_item(
        self, item: ListingItem, profile: SearchProfile, now: datetime
    ) -> bool:
        if item.is_auction:
            item = replace(item, price=item.effective_price)
        price_eur = self._convert(item.price.amount_minor, item.price.currency)
        shipping_eur = self._convert(
            item.shipping.cost.amount_minor if item.shipping.cost else None,
            item.shipping.cost.currency if item.shipping.cost else None,
        )
        import_eur = self._convert(
            item.shipping.import_charges.amount_minor
            if item.shipping.import_charges
            else None,
            item.shipping.import_charges.currency
            if item.shipping.import_charges
            else None,
        )
        current_bid_eur = self._convert(
            item.current_bid.amount_minor if item.current_bid else None,
            item.current_bid.currency if item.current_bid else None,
        )
        condition_class = condition_class_from_ebay(item.condition_label)
        buying_option = "auction" if item.is_auction else "fixed"
        ingested = IngestedListing(
            item=item,
            normalized_title=normalize_text(item.title),
            condition_class=condition_class,
            buying_option=buying_option,
            price_eur_minor=price_eur,
            shipping_eur_minor=shipping_eur,
            import_charges_eur_minor=import_eur,
            current_bid_eur_minor=current_bid_eur,
        )
        result = self._repo.ingest_listing(ingested, now)
        record = result.listing
        created = result.created

        match = self._matcher.match_title(record.title)
        risk = self._classify(record, match)
        category = self._category_for(record, match, risk, profile)
        if category is None:
            return created
        self._save_classification(record, match, risk, now)
        record = self._with_classification(record, match, risk)

        preliminary = self._valuate_and_score(record, match, risk, category, now)
        if self._should_fetch_detail(record, match, risk, category, preliminary):
            record = self._fetch_and_save_detail(record, now)
            if record is None:  # pragma: no cover - only for type checkers
                return created
            match = self._matcher.match_title(record.title)
            risk = self._classify(record, match)
            category = self._category_for(record, match, risk, profile)
            if category is None:
                return created
            self._save_classification(record, match, risk, now)
            record = self._with_classification(record, match, risk)

        valuation, cost, evaluation = self._valuate_and_score(
            record, match, risk, category, now
        )
        if valuation is not None and cost is not None:
            self._save_evaluation(record, match, risk, valuation, cost, evaluation, now)

        if evaluation.alertable:
            payload = self._build_payload(
                record, match, risk, category, valuation, cost, evaluation, profile
            )
            self._alerts.maybe_create(
                evaluation=evaluation,
                listing=record,
                payload=payload,
                now=now,
            )
        elif created:
            log.debug(
                "new listing retained without alert: %s",
                "; ".join(evaluation.suppress_reasons[:4]),
                extra={"event": "candidate_suppressed", "listing_id": record.item_id},
            )
        return created

    def _classify(self, record: ListingRecord, match: MatchResult) -> RiskAssessment:
        return self._classifier.classify(
            parsed=match.parsed,
            match=match,
            condition_label=record.condition_label,
            condition_class=record.condition_class,
            shipping_known=record.shipping_known,
            ship_to_ok=record.ship_to_ok,
            seller_feedback_pct=record.seller_feedback_pct,
            seller_feedback_score=record.seller_feedback_score,
            detail_text=record.description_excerpt,
        )

    def _category_for(
        self,
        record: ListingRecord,
        match: MatchResult,
        risk: RiskAssessment,
        profile: SearchProfile,
    ) -> CategorySpec | None:
        if risk.is_bundle:
            bundle_category = self._config.categories.get(BUNDLE_CATEGORY)
            if bundle_category is not None:
                return bundle_category
        if match.primary is not None:
            return self._config.categories.get(match.primary.product.category)
        if profile.category_id:
            return self._config.categories.get(profile.category_id)
        return None

    def _save_classification(
        self, record: ListingRecord, match: MatchResult, risk: RiskAssessment, now: datetime
    ) -> None:
        self._repo.save_classification(
            record.item_id,
            product_id=match.primary.product.id if match.primary else None,
            match_confidence=match.overall_confidence or None,
            listing_kind=risk.kind,
            risk_level=risk.risk_level,
            risk_flags=risk.flags,
            now=now,
        )

    @staticmethod
    def _with_classification(
        record: ListingRecord, match: MatchResult, risk: RiskAssessment
    ) -> ListingRecord:
        return replace(
            record,
            product_id=match.primary.product.id if match.primary else None,
            match_confidence=match.overall_confidence or None,
            listing_kind=risk.kind,
            risk_level=risk.risk_level,
            risk_flags=risk.flags,
        )

    # ------------------------------------------------------------------ detail
    def _should_fetch_detail(
        self,
        record: ListingRecord,
        match: MatchResult,
        risk: RiskAssessment,
        category: CategorySpec,
        preliminary: Evaluation,
    ) -> bool:
        if record.detail_fetched_at:
            return False
        if risk.blocked:
            return False
        if match.primary is None and not risk.is_bundle:
            return False
        if preliminary.valuation is None:
            return False
        if not self._detail_budget_available():
            return False
        if preliminary.discount_pct >= category.min_discount_pct - 10:
            return True
        if not record.shipping_known and preliminary.discount_pct >= category.min_discount_pct / 2:
            return True
        return False

    def _detail_budget_available(self) -> bool:
        if self._quota.detail_calls_today() >= self._config.scan.item_detail_daily_cap:
            return False
        return self._quota.status().remaining > 0

    def _fetch_and_save_detail(
        self, record: ListingRecord, now: datetime
    ) -> ListingRecord | None:
        try:
            payload = self._client.get_item(record.item_id, record.marketplace)
        except QuotaExceeded:
            log.info("item detail skipped: API budget reserved for later", extra={"event": "detail_budget"})
            return record
        except EbayError as exc:
            log.warning(
                "item detail failed: %s",
                exc,
                extra={"event": "detail_error", "listing_id": record.item_id},
            )
            return record
        try:
            detail = parse_item_detail(
                payload,
                record.marketplace,
                self._config.delivery.country,
            )
        except EbayParseError as exc:
            log.warning(
                "item detail could not be parsed: %s",
                exc,
                extra={"event": "detail_parse_error", "listing_id": record.item_id},
            )
            return record

        shipping_eur = self._convert(
            detail.shipping.cost.amount_minor if detail.shipping.cost else None,
            detail.shipping.cost.currency if detail.shipping.cost else None,
        )
        shipping_known = detail.shipping.cost is not None or record.shipping_known
        if shipping_eur is None:
            shipping_eur = record.shipping_eur_minor
        ship_to_ok = (
            detail.shipping.ship_to_ok
            if detail.shipping.ship_to_ok is not None
            else record.ship_to_ok
        )
        import_eur = self._convert(
            detail.shipping.import_charges.amount_minor
            if detail.shipping.import_charges
            else None,
            detail.shipping.import_charges.currency
            if detail.shipping.import_charges
            else None,
        )
        if import_eur is None:
            import_eur = record.import_charges_eur_minor
        aspect_text = " ".join(
            value for values in detail.aspects.values() for value in values
        )
        detail_text = " ".join(part for part in (detail.description, aspect_text) if part)
        self._repo.save_detail(
            record.item_id,
            description_excerpt=detail_text[:4000] or None,
            aspects=detail.aspects,
            shipping_eur_minor=shipping_eur,
            shipping_known=shipping_known,
            ship_to_ok=ship_to_ok,
            import_charges_eur_minor=import_eur,
            now=now,
        )
        refreshed = self._repo.get_listing(record.item_id)
        return refreshed

    # ------------------------------------------------------------- valuation
    def _valuate_and_score(
        self,
        record: ListingRecord,
        match: MatchResult,
        risk: RiskAssessment,
        category: CategorySpec,
        now: datetime,
    ) -> tuple[Valuation | None, CostBreakdown | None, Evaluation]:
        valuation = self._valuate(record, match, risk, now)
        cost = self._cost(record, category)
        components = (
            tuple(product.name for product in self._component_products(match))
            if risk.is_bundle
            else ()
        )
        evaluation = self._scorer.evaluate(
            listing=record,
            product=match.primary.product if match.primary else None,
            match_confidence=_effective_match_confidence(match, risk),
            risk=risk,
            valuation=valuation,
            cost=cost,
            category=category,
            now=now,
            components=components,
            detail_fetched=bool(record.detail_fetched_at),
        )
        return valuation, cost, evaluation

    def _valuate(
        self,
        record: ListingRecord,
        match: MatchResult,
        risk: RiskAssessment,
        now: datetime,
    ) -> Valuation | None:
        if risk.blocked:
            return None
        if record.condition_class in ("unknown", "for_parts"):
            return None
        if risk.is_bundle:
            components = self._component_products(match)
            return self._estimator.estimate_bundle(
                components=components,
                condition_class=record.condition_class,
                now=now,
                untested=risk.is_untested,
            )
        if match.primary is None:
            return None
        return self._estimator.estimate(
            product=match.primary.product,
            condition_class=record.condition_class,
            item_id=record.item_id,
            now=now,
            untested_haircut_pct=(
                self._config.ranking.untested_value_haircut_pct
                if risk.is_untested
                else 0.0
            ),
        )

    def _component_products(self, match: MatchResult) -> list[ProductSpec]:
        seen: set[str] = set()
        products: list[ProductSpec] = []
        for candidate in match.components:
            if candidate.product.id in seen:
                continue
            seen.add(candidate.product.id)
            products.append(candidate.product)
        return products

    def _cost(
        self, record: ListingRecord, category: CategorySpec
    ) -> CostBreakdown | None:
        if record.price_eur_minor is None:
            return None
        return compute_cost(
            price_eur_minor=record.price_eur_minor,
            shipping_eur_minor=record.shipping_eur_minor,
            import_charges_eur_minor=record.import_charges_eur_minor,
            seller_country=record.location_country,
            delivery_country=self._config.delivery.country,
            category=category,
            alerts=self._config.alerts,
        )

    def _save_evaluation(
        self,
        record: ListingRecord,
        match: MatchResult,
        risk: RiskAssessment,
        valuation: Valuation,
        cost: CostBreakdown,
        evaluation: Evaluation,
        now: datetime,
    ) -> None:
        payload = {
            "deal_score": evaluation.deal_score,
            "confidence_label": evaluation.confidence_label,
            "confidence_score": evaluation.confidence_score,
            "discount_pct": evaluation.discount_pct,
            "savings_eur_minor": evaluation.savings_eur_minor,
            "risk_level": evaluation.risk_level,
            "risk_flags": list(evaluation.risk_flags),
            "suppressed": list(evaluation.suppress_reasons),
            "notes": list(evaluation.notes),
            "components": list(evaluation.components),
            "valuation": {
                "source_kind": valuation.source_kind,
                "source": valuation.source,
                "sample_size": valuation.sample_size,
                "window_days": valuation.window_days,
                "confidence_label": valuation.confidence_label,
                "provisional": valuation.provisional,
            },
            "cost": {
                "price_eur_minor": cost.price_eur_minor,
                "shipping_eur_minor": cost.shipping_eur_minor,
                "import_charges_eur_minor": cost.import_charges_eur_minor,
                "total_eur_minor": cost.total_eur_minor,
                "shipping_known": cost.shipping_known,
                "import_estimated": cost.import_estimated,
                "notes": list(cost.notes),
            },
        }
        self._repo.save_evaluation(
            record.item_id,
            product_id=match.primary.product.id if match.primary else None,
            condition_class=record.condition_class,
            value_eur_minor=valuation.value_eur_minor,
            source_kind=valuation.source_kind,
            source=valuation.source,
            sample_size=valuation.sample_size,
            window_days=valuation.window_days,
            outliers_removed=valuation.outliers_removed,
            valuation_label=valuation.confidence_label,
            valuation_score=valuation.confidence_score,
            method=valuation.method,
            total_cost_eur_minor=evaluation.total_cost_eur_minor,
            discount_pct=evaluation.discount_pct,
            savings_eur_minor=evaluation.savings_eur_minor,
            max_bid_eur_minor=evaluation.max_bid_eur_minor,
            deal_score=evaluation.deal_score,
            confidence_score=evaluation.confidence_score,
            completeness=evaluation.completeness,
            evaluation_payload=payload,
            now=now,
        )

    def _build_payload(
        self,
        record: ListingRecord,
        match: MatchResult,
        risk: RiskAssessment,
        category: CategorySpec,
        valuation: Valuation | None,
        cost: CostBreakdown | None,
        evaluation: Evaluation,
        profile: SearchProfile,
    ) -> dict[str, Any]:
        if valuation is None or cost is None:
            raise ValueError("payload requires a valuation and a cost breakdown")
        if risk.is_bundle:
            bundle: BundleSpec | None = self._bundles.get(profile.bundle_id or "")
            display_name = bundle.name if bundle else "Mixed hardware lot"
        elif match.primary is not None:
            display_name = match.primary.product.name
        else:
            display_name = record.title
        notes = list(dict.fromkeys((*valuation.notes, *cost.notes, *evaluation.notes)))
        return {
            "listing_id": record.item_id,
            "title": display_name,
            "listing_title": record.title,
            "url": record.url,
            "marketplace": record.marketplace,
            "condition": record.condition_label or record.condition_class,
            "delivery_country": self._config.delivery.country,
            "price_eur_minor": record.price_eur_minor,
            "shipping_eur_minor": cost.shipping_eur_minor,
            "shipping_known": cost.shipping_known,
            "import_charges_eur_minor": cost.import_charges_eur_minor,
            "total_cost_eur_minor": cost.total_eur_minor,
            "value_eur_minor": valuation.value_eur_minor,
            "discount_pct": evaluation.discount_pct,
            "savings_eur_minor": evaluation.savings_eur_minor,
            "confidence_label": evaluation.confidence_label,
            "confidence_score": evaluation.confidence_score,
            "source_kind": valuation.source_kind,
            "source": valuation.source,
            "valuation_method": valuation.method,
            "sample_size": valuation.sample_size,
            "window_days": valuation.window_days,
            "risk_level": evaluation.risk_level,
            "risk_flags": list(evaluation.risk_flags),
            "notes": notes,
            "seller_name": record.seller_name,
            "seller_feedback_pct": record.seller_feedback_pct,
            "seller_feedback_score": record.seller_feedback_score,
            "location_country": record.location_country,
            "ship_to_ok": record.ship_to_ok,
            "buying_option": record.buying_option,
            "is_auction": record.buying_option == "auction",
            "bid_count": record.bid_count,
            "end_at": record.item_end_at,
            "max_bid_eur_minor": evaluation.max_bid_eur_minor,
            "first_seen_at": record.first_seen_at,
            "components": list(evaluation.components),
            "deal_score": evaluation.deal_score,
            "urgency": evaluation.urgency,
            "category": category.id,
        }

    def _convert(self, amount_minor: int | None, currency: str | None) -> int | None:
        if amount_minor is None or not currency:
            return None
        try:
            return self._fx.convert_minor(amount_minor, currency)
        except FxUnavailable as exc:
            log.warning("currency conversion unavailable: %s", exc)
            return None
