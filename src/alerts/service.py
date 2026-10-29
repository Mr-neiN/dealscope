"""Alert creation, deduplication and retry delivery."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from src.alerts.telegram import (
    TelegramError,
    TelegramNotifier,
    TelegramPermanentError,
    TelegramRateLimited,
    format_alert,
)
from src.config import AppConfig
from src.ranking.scorer import Evaluation
from src.storage.repository import AlertRecord, ListingRecord, Repository

log = logging.getLogger(__name__)

MAX_DELIVERY_ATTEMPTS = 6
MAX_ALERT_AGE_HOURS = 24


@dataclass(frozen=True)
class DeliveryStats:
    delivered: int = 0
    failed: int = 0
    retried_later: int = 0


class AlertService:
    def __init__(
        self,
        repository: Repository,
        config: AppConfig,
        notifier: TelegramNotifier | None,
        *,
        shadow_mode: bool,
    ) -> None:
        self._repo = repository
        self._config = config
        self._notifier = notifier
        self._shadow_mode = shadow_mode

    def maybe_create(
        self,
        *,
        evaluation: Evaluation,
        listing: ListingRecord,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> bool:
        if not evaluation.alertable or not evaluation.alert_kind:
            return False
        kind = evaluation.alert_kind
        alert_key = _alert_key(evaluation, listing)

        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        if self._shadow_mode:
            # Keep shadow review bounded, but much larger than the live cap.
            cap = max(self._config.alerts.max_per_day * 5, 100)
            used = self._repo.alert_count_for_day(day, include_shadow=True)
            if used >= cap:
                log.info("shadow alert cap reached", extra={"event": "alert_shadow_cap"})
                return False
        else:
            used = self._repo.alert_count_for_day(day)
            if used >= self._config.alerts.max_per_day:
                log.info(
                    "daily alert cap reached",
                    extra={"event": "alert_daily_cap", "used": used},
                )
                return False

        history = self._repo.alerts_for_listing(listing.item_id)
        suppress_reason = self._dedupe_reason(kind, evaluation, payload, history)
        if suppress_reason:
            log.debug(
                "alert suppressed: %s",
                suppress_reason,
                extra={"event": "alert_deduped", "listing_id": listing.item_id},
            )
            return False

        state = "shadow" if self._shadow_mode else "pending"
        priority = _priority(evaluation)
        created = self._repo.create_alert(
            listing_id=listing.item_id,
            kind=kind,
            alert_key=alert_key,
            state=state,
            payload=payload,
            priority=priority,
            now=now,
        )
        if created is None:
            return False
        if self._shadow_mode:
            log.info(
                "shadow candidate: %s score=%.1f discount=%.1f%% confidence=%s",
                payload.get("title"),
                evaluation.deal_score,
                evaluation.discount_pct,
                evaluation.confidence_label,
                extra={
                    "event": "shadow_candidate",
                    "listing_id": listing.item_id,
                    "url": listing.url,
                },
            )
        else:
            log.info(
                "alert queued: %s",
                payload.get("title"),
                extra={"event": "alert_queued", "listing_id": listing.item_id},
            )
        return True

    def deliver_pending(self, now: datetime, *, limit: int = 10) -> DeliveryStats:
        if self._shadow_mode or self._notifier is None or not self._notifier.configured:
            return DeliveryStats()
        delivered = failed = retried = 0
        # Claimed rows are owned by this process until they reach delivered/
        # failed/pending state; another deliverer cannot double-send them.
        for alert in self._repo.claim_pending_alerts(now, limit=limit):
            age_hours = _age_hours(alert.created_at, now)
            if age_hours is not None and age_hours > MAX_ALERT_AGE_HOURS:
                self._repo.mark_alert_failed(
                    alert.id,
                    error="expired before delivery",
                    next_attempt_at=None,
                    now=now,
                    permanent=True,
                )
                failed += 1
                continue
            try:
                message_id = self._notifier.send(format_alert(alert.payload))
            except TelegramRateLimited as exc:
                delay = exc.retry_after if exc.retry_after is not None else _backoff(alert.attempts)
                self._schedule_retry(alert, now, delay, str(exc))
                retried += 1
                continue
            except TelegramPermanentError as exc:
                self._repo.mark_alert_failed(
                    alert.id,
                    error=str(exc),
                    next_attempt_at=None,
                    now=now,
                    permanent=True,
                )
                failed += 1
                continue
            except TelegramError as exc:
                self._schedule_retry(alert, now, _backoff(alert.attempts), str(exc))
                retried += 1
                continue
            self._repo.mark_alert_delivered(alert.id, message_id=message_id, now=now)
            delivered += 1
        return DeliveryStats(delivered, failed, retried)

    def _schedule_retry(
        self, alert: AlertRecord, now: datetime, delay_seconds: float, error: str
    ) -> None:
        if alert.attempts + 1 >= MAX_DELIVERY_ATTEMPTS:
            self._repo.mark_alert_failed(
                alert.id,
                error=error,
                next_attempt_at=None,
                now=now,
                permanent=True,
            )
            log.warning(
                "alert delivery permanently failed",
                extra={"event": "alert_failed", "alert_id": alert.id},
            )
            return
        self._repo.mark_alert_failed(
            alert.id,
            error=error,
            next_attempt_at=now + timedelta(seconds=min(max(delay_seconds, 30), 3600)),
            now=now,
        )

    def _dedupe_reason(
        self,
        kind: str,
        evaluation: Evaluation,
        payload: Mapping[str, Any],
        history: list[AlertRecord],
    ) -> str | None:
        relevant_states = {"delivered", "pending"}
        if self._shadow_mode:
            relevant_states.add("shadow")
        relevant = [alert for alert in history if alert.state in relevant_states]
        if not relevant:
            return None
        if kind == "auction_ending" and any(
            alert.kind == "auction_ending" for alert in relevant
        ):
            return "auction ending already alerted"
        previous = relevant[0]
        if previous.kind != kind and kind != "auction_ending":
            # A change in alert kind (e.g. bundle vs single) is not enough on
            # its own to re-alert unless the price moved materially.
            pass
        previous_price = _payload_price(previous.payload)
        current_price = payload.get("price_eur_minor")
        if previous_price and current_price:
            drop = (previous_price - int(current_price)) / previous_price * 100
            if drop < self._config.alerts.material_price_drop_pct:
                return "price has not fallen materially"
        previous_risk = str(previous.payload.get("risk_level") or "")
        current_risk = str(payload.get("risk_level") or "")
        previous_flags = tuple(previous.payload.get("risk_flags") or [])
        current_flags = tuple(payload.get("risk_flags") or [])
        if previous_risk == current_risk and previous_flags == current_flags:
            if previous_price and current_price and previous_price <= current_price:
                return "same deal event"
        return None


def _alert_key(evaluation: Evaluation, listing: ListingRecord) -> str:
    raw = (
        f"{evaluation.alert_kind}|{listing.item_id}|{listing.price_eur_minor}|"
        f"{evaluation.value_eur_minor}|{evaluation.risk_level}|"
        f"{','.join(evaluation.risk_flags)}|{listing.item_end_at}|"
        f"{listing.current_bid_eur_minor}"
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _payload_price(payload: Mapping[str, Any]) -> int | None:
    try:
        value = payload.get("price_eur_minor")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _priority(evaluation: Evaluation) -> int:
    if evaluation.alert_kind == "auction_ending":
        if evaluation.urgency >= 0.9:
            return 120
        if evaluation.urgency >= 0.75:
            return 100
        return 80
    if evaluation.alert_kind == "bundle_deal":
        return 50
    return 40


def _backoff(attempts: int) -> float:
    return min(3600.0, 60.0 * (2 ** max(0, min(attempts, 6))))


def _age_hours(timestamp: str, now: datetime) -> float | None:
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds() / 3600.0


def alert_payload_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False)
