"""Evidence-aware price estimation.

The estimate is the median of comparable values, never a median across mixed
search results. Every result records its source, sample size, window and
confidence so notifications can explain themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from src.config import AppConfig, ProductSpec
from src.pricing.comparables import ComparableEvidence, ComparableService, PricePoint

RECENCY_HALF_LIFE_DAYS = 45.0
SOLD_STRONG_SAMPLE = 8
SOLD_USABLE_SAMPLE = 5
SOLD_THIN_SAMPLE = 3
ACTIVE_MIN_SAMPLE = 3


@dataclass(frozen=True)
class Valuation:
    value_eur_minor: int
    source_kind: str
    source: str
    sample_size: int
    window_days: int
    outliers_removed: int
    confidence_score: float
    confidence_label: str
    method: str
    provisional: bool
    notes: tuple[str, ...] = ()

    @property
    def is_usable_for_normal_alert(self) -> bool:
        return not self.provisional and self.confidence_label in ("medium", "high")


@dataclass(frozen=True)
class _Selected:
    points: tuple[PricePoint, ...]
    source_kind: str
    source: str
    sample_size: int
    window_days: int
    confidence: float
    label: str
    method: str
    provisional: bool


class PriceEstimator:
    def __init__(
        self,
        comparables: ComparableService,
        config: AppConfig,
        *,
        clock: datetime | None = None,
    ) -> None:
        self._comparables = comparables
        self._config = config

    def estimate(
        self,
        *,
        product: ProductSpec,
        condition_class: str,
        item_id: str,
        now: datetime,
        untested_haircut_pct: float = 0.0,
    ) -> Valuation | None:
        evidence = self._comparables.collect(
            product_id=product.id,
            condition_class=condition_class,
            item_id=item_id,
            now=now,
        )
        selected = self._select(evidence, now)
        if selected is None:
            return None

        points = selected.points
        cleaned, outliers = _remove_outliers(points)
        if not cleaned:
            cleaned, outliers = points, 0
        median = _recency_weighted_median(cleaned, now)
        if median is None or median <= 0:
            return None

        value = median
        notes: list[str] = []
        confidence = selected.confidence
        label = selected.label
        if untested_haircut_pct > 0:
            value = int(round(value * (1 - untested_haircut_pct / 100.0)))
            confidence = min(confidence, 0.45)
            label = "low"
            notes.append(
                f"value reduced by {untested_haircut_pct:.0f}% because functionality is unconfirmed"
            )
        if outliers:
            notes.append(f"{outliers} outlier(s) removed using the IQR rule")

        return Valuation(
            value_eur_minor=value,
            source_kind=selected.source_kind,
            source=selected.source,
            sample_size=selected.sample_size,
            window_days=selected.window_days,
            outliers_removed=outliers,
            confidence_score=round(confidence, 3),
            confidence_label=label,
            method=selected.method,
            provisional=selected.provisional,
            notes=tuple(notes),
        )

    def estimate_bundle(
        self,
        *,
        components: Sequence[ProductSpec],
        condition_class: str,
        now: datetime,
        untested: bool = False,
    ) -> Valuation | None:
        if len(components) < 2:
            return None
        component_values: list[tuple[str, int]] = []
        for product in components:
            reference = next(
                (
                    reference
                    for reference in self._config.references
                    if reference.product_id == product.id
                    and reference.condition_class == condition_class
                ),
                None,
            )
            if reference is None:
                continue
            if not reference.verified:
                continue
            if (
                reference.observed_at is not None
                and _age_days(_reference_datetime(reference.observed_at), now) > 365
            ):
                continue
            component_values.append((product.id, reference.price_eur_minor))

        if len(component_values) < 2:
            return None
        base = sum(value for _, value in component_values)
        haircut = self._config.ranking.bundle_haircut_pct
        if untested:
            haircut += self._config.ranking.untested_value_haircut_pct / 2
        value = int(round(base * (1 - haircut / 100.0)))
        if value <= 0:
            return None
        confidence = 0.62 if len(component_values) >= 3 else 0.45
        label = "medium" if confidence >= 0.6 else "low"
        notes = [
            f"{len(component_values)} verified component reference(s)",
            f"{haircut:.0f}% lot/mixed-condition haircut",
        ]
        source_names = ", ".join(product_id for product_id, _ in component_values)
        return Valuation(
            value_eur_minor=value,
            source_kind="bundle_reference",
            source=f"bundle of verified references: {source_names}",
            sample_size=len(component_values),
            window_days=365,
            outliers_removed=0,
            confidence_score=confidence,
            confidence_label=label,
            method="component references with lot haircut",
            provisional=False,
            notes=tuple(notes),
        )

    def _select(self, evidence: ComparableEvidence, now: datetime) -> _Selected | None:
        sold_recent = tuple(
            point
            for point in evidence.sold
            if _age_days(point.observed_at, now) <= evidence.sold_window_days
        )
        sold_sample = sum(point.weight for point in sold_recent)
        if sold_recent:
            if sold_sample >= SOLD_STRONG_SAMPLE and len(sold_recent) >= 3:
                return _Selected(
                    points=sold_recent,
                    source_kind="sold_verified",
                    source=_sold_source(sold_recent),
                    sample_size=sold_sample,
                    window_days=evidence.sold_window_days,
                    confidence=0.9,
                    label="high",
                    method="recency-weighted median of verified sold observations",
                    provisional=False,
                )
            if sold_sample >= SOLD_USABLE_SAMPLE and len(sold_recent) >= 3:
                return _Selected(
                    points=sold_recent,
                    source_kind="sold_verified",
                    source=_sold_source(sold_recent),
                    sample_size=sold_sample,
                    window_days=evidence.sold_window_days,
                    confidence=0.72,
                    label="medium",
                    method="recency-weighted median of verified sold observations",
                    provisional=False,
                )

        reference = evidence.reference
        if reference is not None and reference.verified:
            if reference.observed_at is None:
                return None
            observed = _reference_datetime(reference.observed_at)
            age_days = _age_days(observed, now)
            confidence = 0.68 if age_days <= 120 else 0.58
            label = "medium" if confidence >= 0.6 else "low"
            return _Selected(
                points=(
                    PricePoint(
                        price_eur_minor=reference.price_eur_minor,
                        observed_at=observed,
                        weight=max(1, reference.sample_size),
                        label=reference.source,
                    ),
                ),
                source_kind="reference",
                source=reference.source,
                sample_size=max(1, reference.sample_size),
                window_days=max(1, age_days or 1),
                confidence=confidence,
                label=label,
                method="verified manual reference price",
                provisional=False,
            )

        if sold_recent and sold_sample >= SOLD_THIN_SAMPLE:
            return _Selected(
                points=sold_recent,
                source_kind="sold_verified",
                source=_sold_source(sold_recent),
                sample_size=sold_sample,
                window_days=evidence.sold_window_days,
                confidence=0.45,
                label="low",
                method="median of a thin verified sold sample",
                provisional=False,
            )

        if reference is not None:
            observed = _reference_datetime(reference.observed_at) if reference.observed_at else now
            return _Selected(
                points=(
                    PricePoint(
                        price_eur_minor=reference.price_eur_minor,
                        observed_at=observed,
                        weight=1,
                        label=reference.source,
                    ),
                ),
                source_kind="reference_unverified",
                source=reference.source,
                sample_size=1,
                window_days=max(1, _age_days(observed, now)),
                confidence=0.3,
                label="provisional",
                method="unverified manual reference price - supporting evidence only",
                provisional=True,
            )

        if len(evidence.active) >= ACTIVE_MIN_SAMPLE:
            return _Selected(
                points=evidence.active,
                source_kind="active_listings",
                source=f"{len(evidence.active)} active asking prices",
                sample_size=len(evidence.active),
                window_days=evidence.active_window_days,
                confidence=0.25,
                label="provisional",
                method="median of active asking prices - not sold evidence",
                provisional=True,
            )
        return None


def _sold_source(points: Sequence[PricePoint]) -> str:
    references = sorted({point.label for point in points if point.label})
    if len(references) == 1:
        return references[0]
    return f"{len(points)} verified sold observation(s)"


def _recency_weighted_median(points: Sequence[PricePoint], now: datetime) -> int | None:
    weighted: list[tuple[int, float]] = []
    for point in points:
        age = max(0.0, _age_days(point.observed_at, now))
        recency = 0.5 ** (age / RECENCY_HALF_LIFE_DAYS)
        weight = max(1, point.weight) * recency
        weighted.append((point.price_eur_minor, weight))
    if not weighted:
        return None
    weighted.sort(key=lambda pair: pair[0])
    total = sum(weight for _, weight in weighted)
    if total <= 0:
        return None
    cumulative = 0.0
    for price, weight in weighted:
        cumulative += weight
        if cumulative >= total / 2:
            return price
    return weighted[-1][0]


def _remove_outliers(points: Sequence[PricePoint]) -> tuple[tuple[PricePoint, ...], int]:
    if len(points) < 5:
        return tuple(points), 0
    values = sorted(point.price_eur_minor for point in points)
    q1 = _percentile(values, 0.25)
    q3 = _percentile(values, 0.75)
    iqr = q3 - q1
    if iqr <= 0:
        return tuple(points), 0
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    kept = tuple(point for point in points if lower <= point.price_eur_minor <= upper)
    if len(kept) < max(3, len(points) // 2):
        return tuple(points), 0
    return kept, len(points) - len(kept)


def _percentile(sorted_values: Sequence[int], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _reference_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        from datetime import date

        if isinstance(value, date):
            parsed = datetime.combine(value, datetime.min.time())
        else:
            parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_days(when: datetime, now: datetime) -> float:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (now - when).total_seconds() / 86400.0)
