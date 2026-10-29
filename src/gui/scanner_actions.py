"""Manual eBay scans and Telegram tests triggered from the dashboard.

The dashboard stays read-only over the database except for manual decisions;
these actions are the deliberate exception. They reuse the exact same
machinery as the continuous scanner:

- the same :class:`Scanner` pipeline (discovery -> ingestion -> matching ->
  valuation -> alert), including shadow-mode handling;
- the same persisted ``job_state`` claims, so a manual scan can never run
  the same search while the scheduler is already running it;
- the same :class:`QuotaLedger`, so a manual scan consumes the day's API
  budget honestly and stops before the reserve.

A manual scan deliberately does not take the ``runner_lock`` lease: that
lease owns the daily schedule (digests, deliveries, availability sweeps),
while a one-off scan only claims individual jobs.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Sequence

from src.discovery.queries import SearchOutcome, SearchProfile, build_profiles
from src.ebay.quota import QuotaExceeded

if TYPE_CHECKING:  # the Runtime lives in main, which imports this lazily
    from src.main import Runtime

log = logging.getLogger(__name__)

JOB_LOCK_SECONDS = 900

SCAN_KINDS = (
    "new_listings",
    "auctions",
    "variant",
    "bundle",
    "discovery",
    "product",
)

MAX_PROFILES_PER_SCAN = 25
DEFAULT_PROFILES_PER_SCAN = 8


class ScanBusy(RuntimeError):
    """A scan is already in progress in this dashboard process."""


@dataclass
class ScanState:
    running: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    requested: dict[str, Any] = field(default_factory=dict)
    profile_results: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "requested": self.requested,
            "profile_results": self.profile_results,
            "summary": self.summary,
        }


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class ScanRunner:
    """Runs one-off profile scans in a worker thread, one at a time."""

    def __init__(
        self,
        config: Any,
        *,
        runtime_factory: Callable[[Any], "Runtime"] | None = None,
    ) -> None:
        # ``config`` is the loaded AppConfig; a None value defers loading to
        # the first trigger so an invalid .env does not break dashboard boot.
        self._config = config
        self._runtime_factory = runtime_factory or _default_runtime_factory
        self._lock = threading.Lock()
        self._state = ScanState()

    # ------------------------------------------------------------------ API
    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._state.as_dict()

    def trigger(self, kind: str, product_id: str | None = None, limit: int = DEFAULT_PROFILES_PER_SCAN) -> dict[str, Any]:
        if kind not in SCAN_KINDS:
            raise ValueError(
                f"unknown scan kind {kind!r}; expected one of {', '.join(SCAN_KINDS)}"
            )
        limit = max(1, min(int(limit), MAX_PROFILES_PER_SCAN))
        if kind == "product":
            product_id = (product_id or "").strip()
            if not product_id:
                raise ValueError("scan kind 'product' requires product_id")
        with self._lock:
            if self._state.running:
                raise ScanBusy("a scan is already running; wait for it to finish")
            self._state = ScanState(running=True, started_at=_utc_iso(_utc_now()))
            self._state.requested = {"kind": kind, "product_id": product_id, "limit": limit}
        thread = threading.Thread(target=self._run, args=(kind, product_id, limit), name="gui-manual-scan", daemon=True)
        thread.start()
        return {"ok": True, "running": True, **self._state.as_dict()}

    def test_notify(self) -> dict[str, Any]:
        """Send a Telegram test message; problems surface as structured errors.

        Returns ``{"ok": ..., "error"/"delivered"}`` instead of raising so
        the dashboard shows a readable message instead of HTTP 500.
        """
        try:
            runtime = self._build_runtime()
        except Exception as exc:
            return {"ok": False, "error": f"configuration problem: {exc}"}
        try:
            runtime.notifier.send(
                "DealScope dashboard: Telegram test message. If you read this"
                " from the chat, alerts reach the right conversation."
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            runtime.close()
        return {"ok": True, "delivered": True}

    # ---------------------------------------------------------------- internals
    def _build_runtime(self) -> "Runtime":
        if self._config is None:
            from src.config import load_config

            self._config = load_config()
        return self._runtime_factory(self._config)

    def select_profiles(self, config: Any, kind: str, product_id: str | None, limit: int) -> list[SearchProfile]:
        profiles = build_profiles(config)
        if kind == "product":
            return [p for p in profiles if p.product_id == product_id][:limit]
        return [p for p in profiles if p.strategy == kind][:limit]

    def _run(self, kind: str, product_id: str | None, limit: int) -> None:
        try:
            runtime = self._build_runtime()
        except Exception as exc:  # report, do not kill the dashboard thread
            self._finish(error=f"scan setup failed: {exc}")
            return
        try:
            if runtime.scanner is None or runtime.quota is None:
                raise RuntimeError("runtime has no eBay scanner; check EBAY credentials")
            config = runtime.config
            profiles = self.select_profiles(config, kind, product_id, limit)
            if not profiles:
                self._finish(summary={"error": "no search profiles match this request"})
                return
            results: list[dict[str, Any]] = []
            summary: dict[str, Any] = {
                "profiles": len(profiles),
                "succeeded": 0,
                "failed": 0,
                "busy": 0,
                "skipped_budget": 0,
            }
            stopped_reason: str | None = None
            for profile in profiles:
                now = _utc_now()
                remaining = runtime.quota.status().remaining
                if remaining <= 0:
                    results.append(_skip_row(profile, "budget exhausted for today"))
                    summary["skipped_budget"] += 1
                    stopped_reason = "daily API budget exhausted"
                    break
                if not runtime.repository.acquire_job(profile.key, now, JOB_LOCK_SECONDS):
                    results.append(_skip_row(profile, "busy: another process is running this search"))
                    summary["busy"] += 1
                    continue
                run_id = runtime.repository.start_search_run(
                    job_key=profile.key,
                    strategy=profile.strategy,
                    query=profile.query,
                    marketplace=profile.marketplace,
                    product_id=profile.product_id,
                    now=now,
                )
                started = now
                try:
                    outcome = runtime.scanner.run_search(profile)
                except QuotaExceeded as exc:
                    runtime.repository.finish_search_run(run_id, now=_utc_now(), outcome="quota_exhausted", error=str(exc))
                    runtime.repository.finish_job(profile.key, now=_utc_now(), success=False, error=str(exc))
                    results.append(_error_row(profile, "quota exhausted"))
                    summary["failed"] += 1
                    stopped_reason = "daily API budget exhausted"
                    break
                except Exception as exc:  # mirror scheduler: record, keep going
                    runtime.repository.finish_search_run(
                        run_id, now=_utc_now(), outcome="error", error=str(exc)
                    )
                    runtime.repository.finish_job(profile.key, now=_utc_now(), success=False, error=str(exc))
                    results.append(_error_row(profile, f"{type(exc).__name__}: {exc}"))
                    summary["failed"] += 1
                    continue
                runtime.repository.finish_search_run(
                    run_id,
                    now=_utc_now(),
                    outcome=outcome.status,
                    http_status=outcome.http_status,
                    items_returned=outcome.items_seen,
                    new_items=outcome.new_items,
                    pages=outcome.pages,
                    error=outcome.error,
                )
                runtime.repository.finish_job(
                    profile.key, now=_utc_now(), success=outcome.status == "ok", error=outcome.error
                )
                results.append(
                    {
                        "job": profile.key,
                        "strategy": profile.strategy,
                        "marketplace": profile.marketplace,
                        "query": profile.query,
                        "outcome": outcome.status,
                        "items": outcome.items_seen,
                        "new": outcome.new_items,
                        "pages": outcome.pages,
                        "duration_s": round((_utc_now() - started).total_seconds(), 2),
                        "error": outcome.error,
                        "warnings": list(outcome.warnings)[:5],
                    }
                )
                if outcome.status == "ok":
                    summary["succeeded"] += 1
                else:
                    summary["failed"] += 1
            delivery = runtime.alerts.deliver_pending(_utc_now())
            summary["alerts"] = {
                "delivered": getattr(delivery, "delivered", 0),
                "failed": getattr(delivery, "failed", 0),
                "retried_later": getattr(delivery, "retried_later", 0),
            }
            try:
                summary["quota_remaining"] = runtime.quota.status().remaining
            except Exception:
                pass
            if stopped_reason:
                summary["stopped"] = stopped_reason
            self._finish(profile_results=results, summary=summary)
        except Exception as exc:
            log.exception("manual scan failed")
            self._finish(error=f"scan failed: {type(exc).__name__}: {exc}")
        finally:
            runtime.close()

    def _finish(self, *, profile_results: list[dict[str, Any]] | None = None, summary: dict[str, Any] | None = None, error: str | None = None) -> None:
        with self._lock:
            if profile_results is not None:
                self._state.profile_results = profile_results
            if summary is not None:
                self._state.summary = summary
            self._state.running = False
            self._state.finished_at = _utc_iso(_utc_now())
            if error:
                self._state.error = error


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _skip_row(profile: SearchProfile, reason: str) -> dict[str, Any]:
    return {
        "job": profile.key,
        "strategy": profile.strategy,
        "marketplace": profile.marketplace,
        "query": profile.query,
        "outcome": "skipped",
        "items": 0,
        "new": 0,
        "pages": 0,
        "error": reason,
    }


def _error_row(profile: SearchProfile, error: str) -> dict[str, Any]:
    row = _skip_row(profile, error)
    row["outcome"] = "error"
    return row


def _default_runtime_factory(config: Any) -> "Runtime":
    # Imported lazily: src.main imports src.gui.server, which must not
    # transitively import src.main at module load time.
    from src.main import build_runtime

    return build_runtime(config, require_ebay=True)
