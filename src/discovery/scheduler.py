"""Sequential, budget-aware scan scheduler.

One process runs one job at a time. A persisted job lock prevents overlapping
executions after restarts. High-priority jobs run first and keep their share of
the budget when the day gets tight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, Sequence

from src.config import AppConfig
from src.discovery.queries import SchedulePlan, SearchOutcome, SearchProfile
from src.ebay.auth import EbayAuthError
from src.ebay.client import EbayError, EbayRateLimited
from src.ebay.quota import QuotaExceeded, QuotaLedger
from src.storage.repository import Repository

log = logging.getLogger(__name__)


class SearchExecutor(Protocol):
    def run_search(self, profile: SearchProfile) -> SearchOutcome: ...


@dataclass(frozen=True)
class CycleStats:
    due: int = 0
    started: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_budget: int = 0


class Scheduler:
    def __init__(
        self,
        profiles: Sequence[SearchProfile],
        plan: SchedulePlan,
        repository: Repository,
        executor: SearchExecutor,
        config: AppConfig,
        quota: QuotaLedger,
        *,
        clock: datetime | None = None,
    ) -> None:
        self._profiles = list(profiles)
        self._plan = plan
        self._repo = repository
        self._executor = executor
        self._config = config
        self._quota = quota
        self._lock_seconds = 900

    def prepare(self) -> None:
        for profile in self._profiles:
            self._repo.upsert_job(
                profile.key,
                profile.strategy,
                profile.query,
                profile.marketplace,
                self._plan.intervals.get(profile.key, profile.base_interval_seconds),
                profile.priority,
            )

    def tick(self, now: datetime, *, max_jobs: int = 10) -> CycleStats:
        states = self._repo.get_job_states()
        due: list[tuple[SearchProfile, datetime | None]] = []
        for profile in self._profiles:
            state = states.get(profile.key)
            last_started = _parse_iso(state.get("last_started_at")) if state else None
            interval = self._plan.intervals.get(profile.key, profile.base_interval_seconds)
            delay = interval
            failures = int(state.get("consecutive_failures") or 0) if state else 0
            if failures:
                delay = min(interval, max(60, 60 * (2 ** min(failures, 6))))
            if last_started is None or (now - last_started).total_seconds() >= delay:
                due.append((profile, last_started))

        due.sort(
            key=lambda pair: (
                pair[0].priority_rank,
                pair[1] or datetime.min.replace(tzinfo=timezone.utc),
                pair[0].key,
            )
        )
        stats = CycleStats(due=len(due))
        usable = max(
            1,
            self._config.effective_daily_budget
            - self._config.budget.reserve_calls,
        )
        for profile, _ in due:
            if self._quota.status().remaining <= 0:
                stats = _bump(stats, skipped=1)
                break
            if not self._budget_allows(profile, usable):
                stats = _bump(stats, skipped=1)
                continue
            if not self._repo.acquire_job(profile.key, now, self._lock_seconds):
                continue
            run_id = self._repo.start_search_run(
                job_key=profile.key,
                strategy=profile.strategy,
                query=profile.query,
                marketplace=profile.marketplace,
                product_id=profile.product_id,
                now=now,
            )
            started_at = now
            try:
                outcome = self._executor.run_search(profile)
            except QuotaExceeded as exc:
                self._repo.finish_search_run(
                    run_id, now=now, outcome="quota_exhausted", error=str(exc)
                )
                self._repo.finish_job(profile.key, now=now, success=False, error=str(exc))
                log.warning("daily quota exhausted", extra={"event": "quota_exhausted"})
                stats = _bump(stats, started=1, failed=1)
                break
            except EbayAuthError as exc:
                self._repo.finish_search_run(
                    run_id, now=now, outcome="auth_error", error=str(exc)
                )
                self._repo.finish_job(profile.key, now=now, success=False, error=str(exc))
                log.error("eBay authentication failed", extra={"event": "auth_error"})
                stats = _bump(stats, started=1, failed=1)
                break
            except EbayRateLimited as exc:
                self._repo.finish_search_run(
                    run_id, now=now, outcome="rate_limited", error=str(exc)
                )
                self._repo.finish_job(profile.key, now=now, success=False, error=str(exc))
                log.warning(
                    "rate limited; pausing the cycle",
                    extra={"event": "rate_limited", "job": profile.key},
                )
                stats = _bump(stats, started=1, failed=1)
                break
            except EbayError as exc:
                self._repo.finish_search_run(
                    run_id, now=now, outcome="api_error", error=str(exc)
                )
                self._repo.finish_job(profile.key, now=now, success=False, error=str(exc))
                log.warning(
                    "search failed: %s",
                    exc,
                    extra={"event": "search_error", "job": profile.key},
                )
                stats = _bump(stats, started=1, failed=1)
                continue
            except Exception as exc:  # boundary: never let one job kill the loop
                self._repo.finish_search_run(
                    run_id, now=now, outcome="internal_error", error=str(exc)
                )
                self._repo.finish_job(profile.key, now=now, success=False, error=str(exc))
                log.exception(
                    "unexpected search failure",
                    extra={"event": "search_exception", "job": profile.key},
                )
                stats = _bump(stats, started=1, failed=1)
                continue

            success = outcome.status == "ok"
            self._repo.finish_search_run(
                run_id,
                now=now,
                outcome=outcome.status,
                http_status=outcome.http_status,
                items_returned=outcome.items_seen,
                new_items=outcome.new_items,
                pages=outcome.pages,
                error=outcome.error,
            )
            self._repo.finish_job(
                profile.key, now=now, success=success, error=outcome.error
            )
            elapsed = (now - started_at).total_seconds()
            log.info(
                "search complete",
                extra={
                    "event": "search_complete",
                    "job": profile.key,
                    "items": outcome.items_seen,
                    "new": outcome.new_items,
                    "pages": outcome.pages,
                    "duration_s": round(elapsed, 2),
                },
            )
            stats = _bump(stats, started=1, succeeded=1 if success else 0, failed=0 if success else 1)
            if stats.started >= max_jobs:
                break
        return stats

    def _budget_allows(self, profile: SearchProfile, usable: int) -> bool:
        remaining = self._quota.status().remaining
        if remaining <= 0:
            return False
        if profile.priority == "high":
            return True
        if self._quota.status().remaining <= max(1, int(usable * 0.20)):
            return profile.priority == "high"
        if profile.priority == "low" and remaining <= max(1, int(usable * 0.40)):
            return False
        return True


def _bump(stats: CycleStats, *, started: int = 0, succeeded: int = 0, failed: int = 0, skipped: int = 0) -> CycleStats:
    return CycleStats(
        due=stats.due,
        started=stats.started + started,
        succeeded=stats.succeeded + succeeded,
        failed=stats.failed + failed,
        skipped_budget=stats.skipped_budget + skipped,
    )


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
