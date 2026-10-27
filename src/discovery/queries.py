"""Search-profile construction and budget-aware interval planning.

Profiles are stable objects derived from configuration. The planner stretches
their target intervals when the requested coverage would exceed the daily API
budget; it never silently spends beyond the configured limit.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Sequence

from src.config import AppConfig, PRIORITIES

PRIORITY_MULTIPLIERS = {"high": 1, "normal": 2, "low": 4}


@dataclass(frozen=True)
class SearchProfile:
    key: str
    strategy: str
    query: str
    marketplace: str
    sort: str
    base_interval_seconds: int
    priority: str
    product_id: str | None = None
    bundle_id: str | None = None
    category_id: str | None = None
    taxonomy_hint: str | None = None
    use_category_filter: bool = False
    max_pages: int = 1

    @property
    def priority_rank(self) -> int:
        return PRIORITIES.index(self.priority)


@dataclass(frozen=True)
class SchedulePlan:
    intervals: dict[str, int]
    planned_calls_per_day: int
    available_calls_per_day: int
    scale_factor: float
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchOutcome:
    status: str
    items_seen: int = 0
    new_items: int = 0
    pages: int = 0
    http_status: int | None = None
    warnings: tuple[str, ...] = ()
    error: str | None = None


def build_profiles(config: AppConfig) -> list[SearchProfile]:
    profiles: list[SearchProfile] = []
    seen: set[tuple[str, str, str]] = set()

    def add(profile: SearchProfile) -> None:
        signature = (profile.marketplace, profile.query.casefold(), profile.sort)
        if signature in seen:
            return
        seen.add(signature)
        profiles.append(profile)

    new_interval = config.scan.new_listings_minutes * 60
    auction_interval = config.scan.auctions_minutes * 60
    variant_interval = config.scan.variant_searches_hours * 3600
    bundle_interval = config.scan.bundle_searches_hours * 3600
    discovery_interval = config.scan.discovery_hours * 3600

    for product in config.products:
        multiplier = PRIORITY_MULTIPLIERS[product.priority]
        primary_query = product.queries[0]
        for marketplace in config.marketplaces:
            add(
                _profile(
                    strategy="new_listings",
                    query=primary_query,
                    marketplace=marketplace,
                    sort="newlyListed",
                    interval=new_interval * multiplier,
                    priority=product.priority,
                    product_id=product.id,
                    category_id=product.category,
                    max_pages=config.scan.max_pages if product.priority == "high" else 1,
                )
            )
            auction_query = (
                product.auction_queries[0] if product.auction_queries else primary_query
            )
            add(
                _profile(
                    strategy="auctions",
                    query=auction_query,
                    marketplace=marketplace,
                    sort="endingSoonest",
                    interval=auction_interval * multiplier,
                    priority=product.priority,
                    product_id=product.id,
                    category_id=product.category,
                    max_pages=1,
                )
            )
            variant_queries = list(product.variant_queries) + list(product.queries[1:])
            for variant in variant_queries:
                add(
                    _profile(
                        strategy="variant",
                        query=variant,
                        marketplace=marketplace,
                        sort="newlyListed",
                        interval=variant_interval,
                        priority=product.priority,
                        product_id=product.id,
                        category_id=product.category,
                        max_pages=1,
                    )
                )
    for bundle in config.bundles:
        multiplier = PRIORITY_MULTIPLIERS[bundle.priority]
        category = config.categories[bundle.category]
        for marketplace in config.marketplaces:
            for query in bundle.queries:
                add(
                    _profile(
                        strategy="bundle",
                        query=query,
                        marketplace=marketplace,
                        sort="newlyListed",
                        interval=bundle_interval * multiplier,
                        priority=bundle.priority,
                        bundle_id=bundle.id,
                        category_id=bundle.category,
                        taxonomy_hint=category.taxonomy_hint,
                        use_category_filter=bundle.use_category_filter,
                        max_pages=1,
                    )
                )

    if config.scan.broad_discovery_enabled:
        for broad in config.broad_searches:
            category = config.categories[broad.category]
            for marketplace in broad.marketplaces:
                add(
                    _profile(
                        strategy="discovery",
                        query=broad.query,
                        marketplace=marketplace,
                        sort="newlyListed",
                        interval=discovery_interval,
                        priority="low",
                        category_id=broad.category,
                        taxonomy_hint=category.taxonomy_hint,
                        use_category_filter=broad.use_category_filter,
                        max_pages=1,
                    )
                )

    return sorted(profiles, key=lambda profile: (profile.priority_rank, profile.key))


def plan_schedule(profiles: Sequence[SearchProfile], config: AppConfig) -> SchedulePlan:
    budget = config.effective_daily_budget
    available = max(1, budget - config.budget.reserve_calls - config.scan.item_detail_daily_cap)
    base_calls = sum(
        profile.max_pages * 86400.0 / profile.base_interval_seconds for profile in profiles
    )
    factor = max(1.0, base_calls / available)
    intervals: dict[str, int] = {}
    warnings: list[str] = []
    max_interval = 7 * 86400

    for profile in profiles:
        target = int(math.ceil(profile.base_interval_seconds * factor))
        if target > max_interval:
            warnings.append(
                f"{profile.key}: interval capped at 7 days (would need {target}s)"
            )
            target = max_interval
        intervals[profile.key] = target

    planned = sum(
        profile.max_pages * 86400.0 / intervals[profile.key] for profile in profiles
    )
    planned_calls = int(math.ceil(planned))
    if planned_calls > available:
        warnings.append(
            f"planned calls ({planned_calls}) still exceed the search budget ({available}); "
            "low-priority jobs will be skipped when the budget is tight"
        )
    if factor > 1.0:
        warnings.append(
            f"target intervals stretched by {factor:.2f}x to fit the daily API budget"
        )
    return SchedulePlan(
        intervals=intervals,
        planned_calls_per_day=planned_calls,
        available_calls_per_day=available,
        scale_factor=round(factor, 3),
        warnings=tuple(warnings),
    )


def _profile(
    *,
    strategy: str,
    query: str,
    marketplace: str,
    sort: str,
    interval: int,
    priority: str,
    product_id: str | None = None,
    bundle_id: str | None = None,
    category_id: str | None = None,
    taxonomy_hint: str | None = None,
    use_category_filter: bool = False,
    max_pages: int = 1,
) -> SearchProfile:
    identity = product_id or bundle_id or category_id or "general"
    digest = hashlib.sha1(query.casefold().encode("utf-8")).hexdigest()[:8]
    key = f"{strategy}:{marketplace}:{identity}:{digest}:{sort}"
    return SearchProfile(
        key=key,
        strategy=strategy,
        query=query,
        marketplace=marketplace,
        sort=sort,
        base_interval_seconds=max(interval, 60),
        priority=priority,
        product_id=product_id,
        bundle_id=bundle_id,
        category_id=category_id,
        taxonomy_hint=taxonomy_hint,
        use_category_filter=use_category_filter,
        max_pages=max(1, max_pages),
    )
