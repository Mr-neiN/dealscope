"""Command-line entry point for the eBay hardware deal scanner."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import httpx

from src.alerts.service import AlertService
from src.alerts.telegram import TelegramNotifier
from src.config import (
    CONDITION_CLASSES,
    AppConfig,
    ConfigError,
    Settings,
    load_config,
)
from src.discovery.queries import SchedulePlan, SearchProfile, build_profiles, plan_schedule
from src.discovery.scheduler import Scheduler
from src.ebay.auth import EbayAuth, EbayAuthError
from src.ebay.client import EbayClient
from src.ebay.models import EbayParseError, parse_item_detail
from src.ebay.quota import QuotaLedger
from src.ebay.taxonomy import TaxonomyService
from src.monitoring.logging_setup import configure_logging
from src.monitoring.metrics import generate_report, render_report
from src.money import MoneyError, parse_eur_minor
from src.pipeline import Scanner
from src.pricing.comparables import ComparableService
from src.pricing.estimator import PriceEstimator
from src.pricing.fx import FxService
from src.products.exclusions import ListingClassifier
from src.products.matcher import ProductMatcher
from src.ranking.scorer import DealScorer
from src.storage.database import Database
from src.storage.repository import Repository, utc_now

log = logging.getLogger(__name__)

DECISIONS = (
    "good_deal",
    "purchased",
    "wrong_product",
    "defective",
    "unavailable",
    "too_risky",
    "not_needed",
)
BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Runtime:
    config: AppConfig
    database: Database
    repository: Repository
    matcher: ProductMatcher
    classifier: ListingClassifier
    estimator: PriceEstimator
    scorer: DealScorer
    notifier: TelegramNotifier
    alerts: AlertService
    http: httpx.Client | None = None
    auth: EbayAuth | None = None
    quota: QuotaLedger | None = None
    client: EbayClient | None = None
    taxonomy: TaxonomyService | None = None
    fx: FxService | None = None
    scanner: Scanner | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        elif self.http is not None:
            self.http.close()
        try:
            self.notifier.close()
        finally:
            self.repository.close()


def build_runtime(config: AppConfig, *, require_ebay: bool) -> Runtime:
    database = Database(config.settings.database_path)
    database.initialize()
    repository = Repository(database)
    repository.seed_products(_product_rows(config))

    matcher = ProductMatcher(config.products, config.variant_suffixes)
    classifier = ListingClassifier(
        config.exclusions,
        low_seller_feedback_pct=config.ranking.low_seller_feedback_pct,
    )
    comparables = ComparableService(repository, config)
    estimator = PriceEstimator(comparables, config)
    scorer = DealScorer(config)
    notifier = TelegramNotifier(
        config.settings.telegram_bot_token,
        config.settings.telegram_chat_id,
    )
    alerts = AlertService(
        repository,
        config,
        notifier,
        shadow_mode=config.settings.shadow_mode,
    )

    runtime = Runtime(
        config=config,
        database=database,
        repository=repository,
        matcher=matcher,
        classifier=classifier,
        estimator=estimator,
        scorer=scorer,
        notifier=notifier,
        alerts=alerts,
    )
    if not require_ebay:
        return runtime

    if not config.settings.ebay_configured:
        runtime.close()
        raise ConfigError(
            "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are required. "
            "Create a production key set at https://developer.ebay.com/my/keys "
            "and put it in .env."
        )
    http = httpx.Client(
        timeout=httpx.Timeout(config.settings.http_timeout_seconds),
        follow_redirects=True,
    )
    quota = QuotaLedger(
        repository,
        config.effective_daily_budget,
        config.budget.reserve_calls,
    )
    auth = EbayAuth(config.settings, http)
    client = EbayClient(
        config.settings,
        auth,
        quota,
        http=http,
        max_attempts=config.settings.http_max_attempts,
    )
    taxonomy = TaxonomyService(
        client,
        repository,
        cache_days=config.scan.taxonomy_cache_days,
    )
    fx = FxService(repository, http=http)
    scanner = Scanner(
        config,
        repository,
        client,
        matcher,
        classifier,
        estimator,
        scorer,
        alerts,
        taxonomy,
        fx,
        quota,
    )
    runtime.http = http
    runtime.quota = quota
    runtime.auth = auth
    runtime.client = client
    runtime.taxonomy = taxonomy
    runtime.fx = fx
    runtime.scanner = scanner
    return runtime


def _product_rows(config: AppConfig) -> list[dict[str, Any]]:
    return [
        {
            "id": product.id,
            "name": product.name,
            "category": product.category,
            "priority": product.priority,
            "condition_class": product.condition_class,
            "aliases": list(product.aliases),
            "spec": dict(product.spec),
        }
        for product in config.products
    ]


# --------------------------------------------------------------------- commands
def cmd_run(config: AppConfig, args: argparse.Namespace) -> int:
    if not config.settings.shadow_mode and not config.settings.telegram_configured:
        raise ConfigError(
            "live alerts require TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, "
            "or set SHADOW_MODE=true"
        )
    runtime = build_runtime(config, require_ebay=True)
    try:
        assert runtime.scanner and runtime.quota
        profiles = build_profiles(config)
        plan = plan_schedule(profiles, config)
        scheduler = Scheduler(
            profiles,
            plan,
            runtime.repository,
            runtime.scanner,
            config,
            runtime.quota,
        )
        scheduler.prepare()
        _log_plan(plan, config, profiles)
        if config.settings.shadow_mode:
            log.info(
                "shadow mode is active; candidates are logged but not sent",
                extra={"event": "shadow_mode"},
            )

        stop = threading.Event()
        _install_signal_handlers(stop)
        started_at = utc_now()
        heartbeat_path = _heartbeat_path(config)
        last_stale = started_at - _hours(1)
        last_backup_day: str | None = None
        last_prune_day: str | None = None
        last_stats = None
        last_delivery = None

        while not stop.is_set():
            now = utc_now()
            try:
                last_stats = scheduler.tick(now, max_jobs=8)
            except Exception:  # boundary: keep the service alive and visible
                log.exception("scheduler cycle failed", extra={"event": "cycle_error"})
            try:
                last_delivery = runtime.alerts.deliver_pending(utc_now())
            except Exception:
                log.exception("alert delivery cycle failed", extra={"event": "delivery_error"})

            if (now - last_stale).total_seconds() >= 3600:
                try:
                    stale, unknown = runtime.repository.mark_stale(
                        now=now,
                        stale_hours=config.scan.stale_after_hours,
                        unknown_hours=config.scan.unknown_after_hours,
                    )
                    log.info(
                        "availability sweep complete",
                        extra={"event": "stale_sweep", "not_seen": stale, "unknown": unknown},
                    )
                except Exception:
                    log.exception("availability sweep failed", extra={"event": "stale_error"})
                last_stale = now

            today = now.strftime("%Y-%m-%d")
            if last_prune_day != today:
                try:
                    removed = runtime.repository.prune(
                        now=now,
                        observation_days=config.retention.observation_days,
                        alert_days=config.retention.alert_days,
                        api_call_days=config.retention.api_call_days,
                    )
                    log.info(
                        "retention pruning complete",
                        extra={"event": "prune", **removed},
                    )
                except Exception:
                    log.exception("retention pruning failed", extra={"event": "prune_error"})
                last_prune_day = today

            if last_backup_day != today:
                try:
                    backup_path = _backup_database(runtime, config, now)
                    _prune_backups(config, now)
                    log.info(
                        "database backup complete",
                        extra={"event": "backup", "path": str(backup_path)},
                    )
                except Exception:
                    log.exception("database backup failed", extra={"event": "backup_error"})
                last_backup_day = today

            _write_heartbeat(
                heartbeat_path,
                config=config,
                plan=plan,
                quota=runtime.quota,
                started_at=started_at,
                now=utc_now(),
                stats=last_stats,
                delivery=last_delivery,
            )
            stop.wait(20)

        log.info("scanner stopped", extra={"event": "stopped"})
        _write_heartbeat(
            heartbeat_path,
            config=config,
            plan=plan,
            quota=runtime.quota,
            started_at=started_at,
            now=utc_now(),
            stats=last_stats,
            delivery=last_delivery,
            stopping=True,
        )
        return 0
    finally:
        runtime.close()


def cmd_once(config: AppConfig, args: argparse.Namespace) -> int:
    runtime = build_runtime(config, require_ebay=True)
    try:
        assert runtime.scanner
        profiles = build_profiles(config)
        if args.strategy:
            profiles = [profile for profile in profiles if profile.strategy == args.strategy]
            if not profiles:
                raise ConfigError(
                    f"no profiles for strategy {args.strategy!r}; available: "
                    + ", ".join(sorted({p.strategy for p in build_profiles(config)}))
                )
        selected = profiles[: max(1, args.limit)]
        for profile in selected:
            print(
                f"running {profile.strategy} {profile.marketplace} "
                f"{profile.query!r} ({profile.sort})"
            )
            outcome = runtime.scanner.run_search(profile)
            print(
                f"  {outcome.status}: {outcome.items_seen} items, "
                f"{outcome.new_items} new, {outcome.pages} page(s)"
            )
            if outcome.warnings:
                for warning in outcome.warnings:
                    print(f"  warning: {warning}")
        delivery = runtime.alerts.deliver_pending(utc_now())
        if delivery.delivered or delivery.failed or delivery.retried_later:
            print(
                f"alerts: delivered={delivery.delivered} "
                f"failed={delivery.failed} retried={delivery.retried_later}"
            )
        return 0
    finally:
        runtime.close()


def cmd_feasibility(config: AppConfig, args: argparse.Namespace) -> int:
    runtime = build_runtime(config, require_ebay=True)
    try:
        assert runtime.auth and runtime.client and runtime.quota
        checks: list[dict[str, Any]] = []
        marketplaces_report: list[dict[str, Any]] = []

        def check(name: str, ok: bool | None, detail: str, *, required: bool = True) -> None:
            checks.append(
                {"name": name, "ok": ok, "detail": detail, "required": required}
            )

        try:
            runtime.auth.get_token()
            check(
                "production_token",
                True,
                f"OAuth client-credentials token issued by {config.settings.ebay_environment}",
            )
        except EbayAuthError as exc:
            check("production_token", False, str(exc))
            _finish_feasibility(config, checks, marketplaces_report, runtime, args)
            return 1

        targets = ["EBAY_IT"]
        for marketplace in ("EBAY_DE", "EBAY_AT"):
            if marketplace in config.marketplaces:
                targets.append(marketplace)
        query = config.products[0].queries[0]
        item_fields_ok = True
        delivery_ok: bool | None = None

        for marketplace in targets:
            entry: dict[str, Any] = {"marketplace": marketplace, "query": query}
            try:
                page = runtime.client.search(
                    marketplace=marketplace,
                    query=query,
                    sort="newlyListed",
                    limit=3,
                )
            except Exception as exc:  # feasibility reports the actual failure
                check(
                    f"search_{marketplace}",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
                entry["error"] = f"{type(exc).__name__}: {exc}"
                marketplaces_report.append(entry)
                continue
            entry["items"] = len(page.items)
            check(
                f"search_{marketplace}",
                True,
                f"{len(page.items)} listing(s) returned for {query!r}",
            )
            if not page.items:
                entry["detail"] = "no listings returned"
                marketplaces_report.append(entry)
                if marketplace == "EBAY_IT":
                    item_fields_ok = False
                continue

            item = page.items[0]
            fields_ok = bool(
                item.item_id
                and item.title
                and item.url
                and item.price.amount_minor > 0
                and item.condition_label
            )
            entry["sample_item"] = {
                "item_id": item.item_id,
                "title": item.title,
                "price": str(item.price),
                "condition": item.condition_label,
                "url": item.url,
                "shipping_cost": str(item.shipping.cost) if item.shipping.cost else None,
                "ship_to_ok": item.shipping.ship_to_ok,
                "end_at": item.item_end_at.isoformat() if item.item_end_at else None,
            }
            check(
                f"fields_{marketplace}",
                fields_ok,
                "itemId, title, price, condition and itemWebUrl present"
                if fields_ok
                else "one or more required fields were missing",
            )
            if marketplace == "EBAY_IT":
                item_fields_ok = item_fields_ok and fields_ok

            detail_ok: bool | None = None
            try:
                detail_payload = runtime.client.get_item(item.item_id, marketplace)
                detail = parse_item_detail(
                    detail_payload,
                    marketplace,
                    config.delivery.country,
                    existing=item,
                )
                entry["detail"] = {
                    "description_present": bool(detail.description),
                    "aspect_count": len(detail.aspects),
                    "shipping_options_locations": list(detail.shipping.ship_to_locations),
                    "ship_to_ok": detail.shipping.ship_to_ok,
                }
                detail_ok = bool(detail.description or detail.aspects) and (
                    detail.shipping.cost is not None
                    or detail.shipping.ship_to_ok is not None
                )
                check(
                    f"item_detail_{marketplace}",
                    detail_ok,
                    "description/aspects and at least one shipping signal available"
                    if detail_ok
                    else "item detail was readable but description or shipping was absent",
                    required=False,
                )
                if marketplace == "EBAY_IT" and not fields_ok:
                    item_fields_ok = False
                if marketplace in ("EBAY_DE", "EBAY_AT"):
                    if detail.shipping.ship_to_ok is False:
                        delivery_ok = False
                    elif detail.shipping.ship_to_ok is True:
                        delivery_ok = True if delivery_ok is None else delivery_ok
            except Exception as exc:
                entry["detail_error"] = f"{type(exc).__name__}: {exc}"
                if marketplace in ("EBAY_DE", "EBAY_AT"):
                    delivery_ok = None

            if marketplace != "EBAY_IT":
                ship = item.shipping.ship_to_ok
                if ship is not None:
                    delivery_ok = bool(ship) if delivery_ok is None else delivery_ok and bool(ship)
            marketplaces_report.append(entry)

        check(
            "listing_fields",
            item_fields_ok,
            "usable listing fields verified on the Italian marketplace",
        )
        if "EBAY_DE" in targets or "EBAY_AT" in targets:
            check(
                "delivery_to_italy",
                delivery_ok,
                "delivery to Italy confirmed by item shipping data"
                if delivery_ok is True
                else "only the eBay deliveryCountry filter could confirm shipping"
                if delivery_ok is None
                else "at least one offer did not ship to Italy",
            )

        try:
            category = runtime.taxonomy.category_for(
                "EBAY_IT",
                config.categories["gpu"].taxonomy_hint,
            )
            check(
                "taxonomy_access",
                category is not None,
                f"resolved GPU category: {category}" if category else "no category suggestion returned",
                required=False,
            )
        except Exception as exc:
            check("taxonomy_access", False, f"{type(exc).__name__}: {exc}", required=False)

        profiles = build_profiles(config)
        plan = plan_schedule(profiles, config)
        check(
            "production_access",
            config.settings.ebay_environment == "production",
            "using the production environment" if config.settings.ebay_environment == "production"
            else "running against sandbox; production eligibility is not proven",
        )
        documented = 5000
        allowance_ok = (
            plan.planned_calls_per_day <= plan.available_calls_per_day
            and config.effective_daily_budget <= documented
        )
        check(
            "request_allowance",
            allowance_ok,
            f"planned {plan.planned_calls_per_day} calls/day; "
            f"configured budget {config.effective_daily_budget}/day; "
            f"eBay documents a default {documented}/day for Browse but does not expose "
            "remaining quota in the response",
        )
        if plan.warnings:
            checks.append(
                {
                    "name": "schedule_warnings",
                    "ok": None,
                    "detail": "; ".join(plan.warnings),
                    "required": False,
                }
            )

        if args.telegram:
            try:
                runtime.notifier.send_test()
                check("telegram_test", True, "test notification delivered")
            except Exception as exc:
                check("telegram_test", False, f"{type(exc).__name__}: {exc}", required=False)

        return _finish_feasibility(config, checks, marketplaces_report, runtime, args, plan=plan)
    finally:
        runtime.close()


def _finish_feasibility(
    config: AppConfig,
    checks: list[dict[str, Any]],
    marketplaces: list[dict[str, Any]],
    runtime: Runtime,
    args: argparse.Namespace,
    *,
    plan: SchedulePlan | None = None,
) -> int:
    required_failures = [
        check for check in checks if check["required"] and check["ok"] is False
    ]
    warnings = [check for check in checks if check["ok"] is None]
    report = {
        "generated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "environment": config.settings.ebay_environment,
        "delivery_country": config.delivery.country,
        "overall_ok": not required_failures,
        "checks": checks,
        "marketplaces": marketplaces,
        "plan": {
            "profiles": len(build_profiles(config)),
            "planned_calls_per_day": plan.planned_calls_per_day if plan else None,
            "available_calls_per_day": plan.available_calls_per_day if plan else None,
            "scale_factor": plan.scale_factor if plan else None,
            "daily_budget": config.effective_daily_budget,
            "warnings": list(plan.warnings) if plan else [],
        },
        "api_calls_used": runtime.quota.status().used if runtime.quota else 0,
    }
    output = config.settings.database_path.parent / "feasibility-report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"eBay API feasibility report - {report['generated_at']}")
    for check in checks:
        status = "PASS" if check["ok"] is True else "WARN" if check["ok"] is None else "FAIL"
        print(f"[{status}] {check['name']}: {check['detail']}")
    print(f"\nReport written to {output}")
    if plan is not None:
        print(
            f"Plan: {plan.planned_calls_per_day}/{plan.available_calls_per_day} "
            f"search calls/day (scale {plan.scale_factor}x)"
        )
    if required_failures:
        print("\nRequired checks failed; fix them before enabling live scans.", file=sys.stderr)
        return 1
    if warnings:
        print("\nWarnings are non-blocking but should be reviewed.")
    print("\nFeasibility checks passed.")
    return 0


def cmd_report(config: AppConfig, args: argparse.Namespace) -> int:
    runtime = build_runtime(config, require_ebay=False)
    try:
        plan = plan_schedule(build_profiles(config), config)
        report = generate_report(
            runtime.repository,
            config,
            now=utc_now(),
            days=max(1, args.days),
            plan=plan,
        )
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(render_report(report))
        return 0
    finally:
        runtime.close()


def cmd_plan(config: AppConfig, args: argparse.Namespace) -> int:
    profiles = build_profiles(config)
    plan = plan_schedule(profiles, config)
    if args.json:
        print(
            json.dumps(
                {
                    "budget": config.effective_daily_budget,
                    "reserve_calls": config.budget.reserve_calls,
                    "detail_cap": config.scan.item_detail_daily_cap,
                    "available_calls_per_day": plan.available_calls_per_day,
                    "planned_calls_per_day": plan.planned_calls_per_day,
                    "scale_factor": plan.scale_factor,
                    "warnings": list(plan.warnings),
                    "profiles": [
                        {
                            "key": profile.key,
                            "strategy": profile.strategy,
                            "marketplace": profile.marketplace,
                            "query": profile.query,
                            "sort": profile.sort,
                            "priority": profile.priority,
                            "interval_seconds": plan.intervals.get(
                                profile.key, profile.base_interval_seconds
                            ),
                            "max_pages": profile.max_pages,
                        }
                        for profile in profiles
                    ],
                },
                indent=2,
            )
        )
        return 0
    by_strategy: dict[str, list[SearchProfile]] = {}
    for profile in profiles:
        by_strategy.setdefault(profile.strategy, []).append(profile)
    print(f"Configured budget: {config.effective_daily_budget} calls/day")
    print(
        f"Search allowance: {plan.available_calls_per_day} calls/day "
        f"(reserve {config.budget.reserve_calls}, detail cap {config.scan.item_detail_daily_cap})"
    )
    print(f"Planned searches: {plan.planned_calls_per_day} calls/day")
    print(f"Schedule scale factor: {plan.scale_factor}x")
    print("")
    for strategy, group in sorted(by_strategy.items()):
        intervals = [plan.intervals.get(p.key, p.base_interval_seconds) for p in group]
        print(
            f"{strategy:14} {len(group):3} jobs  "
            f"{min(intervals) // 60:>4}-{max(intervals) // 60:<4} min  "
            f"({', '.join(sorted({p.marketplace for p in group}))})"
        )
    for warning in plan.warnings:
        print(f"\nWARNING: {warning}")
    return 0


def cmd_backup(config: AppConfig, args: argparse.Namespace) -> int:
    runtime = build_runtime(config, require_ebay=False)
    try:
        path = _backup_database(runtime, config, utc_now())
        _prune_backups(config, utc_now())
        print(f"Backup written to {path}")
        return 0
    finally:
        runtime.close()


def cmd_prune(config: AppConfig, args: argparse.Namespace) -> int:
    runtime = build_runtime(config, require_ebay=False)
    try:
        removed = runtime.repository.prune(
            now=utc_now(),
            observation_days=config.retention.observation_days,
            alert_days=config.retention.alert_days,
            api_call_days=config.retention.api_call_days,
        )
        print("Pruned: " + ", ".join(f"{key}={value}" for key, value in removed.items()))
        return 0
    finally:
        runtime.close()


def cmd_add_comparable(config: AppConfig, args: argparse.Namespace) -> int:
    if args.product not in config.product_by_id:
        raise ConfigError(
            f"unknown product {args.product!r}; known: "
            + ", ".join(sorted(config.product_by_id))
        )
    if args.condition not in CONDITION_CLASSES:
        raise ConfigError(f"--condition must be one of {', '.join(CONDITION_CLASSES)}")
    try:
        price = parse_eur_minor(args.price)
    except MoneyError as exc:
        raise ConfigError(f"invalid --price: {exc}") from exc
    if price <= 0:
        raise ConfigError("--price must be positive")
    try:
        observed_at = (
            date.fromisoformat(args.observed_at)
            if args.observed_at
            else utc_now().date()
        )
    except ValueError as exc:
        raise ConfigError("--observed-at must be an ISO date (YYYY-MM-DD)") from exc
    runtime = build_runtime(config, require_ebay=False)
    try:
        comparable_id = runtime.repository.add_comparable(
            product_id=args.product,
            condition_class=args.condition,
            price_eur_minor=price,
            observed_at=datetime.combine(
                observed_at, datetime.min.time(), tzinfo=timezone.utc
            ),
            source_kind="sold_verified" if args.verified else "observation",
            source_ref=args.source,
            verified=bool(args.verified),
            sample_size=max(1, args.sample_size),
            note=args.note,
        )
        print(f"Added comparable #{comparable_id} for {args.product} ({args.condition})")
        if not args.verified:
            print(
                "Not marked verified: the estimator will not use this observation. "
                "Re-import with --verified once you trust the source."
            )
        return 0
    finally:
        runtime.close()


def cmd_record_decision(config: AppConfig, args: argparse.Namespace) -> int:
    if args.decision not in DECISIONS:
        raise ConfigError(f"--decision must be one of {', '.join(DECISIONS)}")
    runtime = build_runtime(config, require_ebay=False)
    try:
        listing = runtime.repository.get_listing(args.item_id)
        if listing is None:
            print(f"Warning: no listing {args.item_id} in the database", file=sys.stderr)
        runtime.repository.record_decision(
            listing_id=args.item_id,
            decision=args.decision,
            reason=args.reason,
        )
        print(f"Recorded {args.decision} for {args.item_id}")
        return 0
    finally:
        runtime.close()


def cmd_notify_test(config: AppConfig, args: argparse.Namespace) -> int:
    if not config.settings.telegram_configured:
        raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    notifier = TelegramNotifier(
        config.settings.telegram_bot_token,
        config.settings.telegram_chat_id,
    )
    try:
        notifier.send_test()
        print("Test notification delivered.")
        return 0
    finally:
        notifier.close()


def cmd_health(config: AppConfig | None = None, args: argparse.Namespace | None = None) -> int:
    from src.healthcheck import main as health_main

    return health_main()


# --------------------------------------------------------------------- helpers
def _install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        log.info("shutdown signal received", extra={"event": "shutdown", "signal": signum})
        stop.set()

    for signal_name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, signal_name):
            try:
                signal.signal(getattr(signal, signal_name), handler)
            except (ValueError, OSError):
                pass


def _hours(value: int) -> Any:
    from datetime import timedelta

    return timedelta(hours=value)


def _heartbeat_path(config: AppConfig) -> Path:
    override = os.environ.get("HEARTBEAT_PATH")
    if override:
        return Path(override)
    return config.settings.database_path.parent / "heartbeat.json"


def _write_heartbeat(
    path: Path,
    *,
    config: AppConfig,
    plan: SchedulePlan,
    quota: QuotaLedger,
    started_at: datetime,
    now: datetime,
    stats: Any,
    delivery: Any,
    stopping: bool = False,
) -> None:
    status = quota.status()
    payload = {
        "updated_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "started_at": started_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "shadow_mode": config.settings.shadow_mode,
        "stopping": stopping,
        "jobs_due": getattr(stats, "due", 0) if stats else 0,
        "jobs_started": getattr(stats, "started", 0) if stats else 0,
        "jobs_failed": getattr(stats, "failed", 0) if stats else 0,
        "alerts_delivered": getattr(delivery, "delivered", 0) if delivery else 0,
        "budget": {
            "day": status.day,
            "used": status.used,
            "budget": status.budget,
            "reserve": status.reserve,
        },
        "schedule": {
            "planned_calls_per_day": plan.planned_calls_per_day,
            "available_calls_per_day": plan.available_calls_per_day,
            "scale_factor": plan.scale_factor,
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        log.exception("could not write heartbeat", extra={"event": "heartbeat_error"})


def _backup_database(runtime: Runtime, config: AppConfig, now: datetime) -> Path:
    directory = config.settings.database_path.parent / "backups"
    destination = directory / f"scanner-{now.strftime('%Y%m%d-%H%M%S')}.db"
    runtime.database.backup(destination)
    return destination


def _prune_backups(config: AppConfig, now: datetime) -> None:
    directory = config.settings.database_path.parent / "backups"
    if not directory.exists():
        return
    cutoff = now.timestamp() - config.retention.backup_days * 86400
    for path in directory.glob("scanner-*.db"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            log.warning("could not prune backup %s", path)


def _log_plan(plan: SchedulePlan, config: AppConfig, profiles: Sequence[SearchProfile]) -> None:
    log.info(
        "schedule planned",
        extra={
            "event": "plan",
            "profiles": len(profiles),
            "planned_calls_per_day": plan.planned_calls_per_day,
            "available_calls_per_day": plan.available_calls_per_day,
            "scale_factor": plan.scale_factor,
            "budget": config.effective_daily_budget,
            "shadow_mode": config.settings.shadow_mode,
        },
    )
    for warning in plan.warnings:
        log.warning(warning, extra={"event": "plan_warning"})


# ------------------------------------------------------------------ entry point
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ebay-scanner",
        description="European eBay hardware deal scanner (manual purchase decisions only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the continuous scheduler")
    run.set_defaults(func=cmd_run)

    once = sub.add_parser("once", help="run one search profile for diagnostics")
    once.add_argument("--strategy", help="new_listings, auctions, variant, bundle or discovery")
    once.add_argument("--limit", type=int, default=1, help="number of profiles to run")
    once.set_defaults(func=cmd_once)

    feasibility = sub.add_parser(
        "feasibility",
        help="verify eBay API access, marketplaces, shipping data and budget",
    )
    feasibility.add_argument(
        "--telegram",
        action="store_true",
        help="also send a Telegram test message",
    )
    feasibility.set_defaults(func=cmd_feasibility)

    report = sub.add_parser("report", help="show alert quality and API usage")
    report.add_argument("--days", type=int, default=7)
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)

    plan = sub.add_parser("plan", help="show derived search schedule and API budget")
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(func=cmd_plan)

    backup = sub.add_parser("backup", help="create a consistent SQLite backup")
    backup.set_defaults(func=cmd_backup)

    prune = sub.add_parser("prune", help="apply retention limits")
    prune.set_defaults(func=cmd_prune)

    comparable = sub.add_parser(
        "add-comparable",
        help="import a sold/observed comparable price",
    )
    comparable.add_argument("--product", required=True)
    comparable.add_argument("--price", required=True, help="price in EUR")
    comparable.add_argument("--condition", default="used_working", choices=CONDITION_CLASSES)
    comparable.add_argument("--source", required=True)
    comparable.add_argument("--verified", action="store_true")
    comparable.add_argument("--observed-at", default=None)
    comparable.add_argument("--sample-size", type=int, default=1)
    comparable.add_argument("--note", default=None)
    comparable.set_defaults(func=cmd_add_comparable)

    decision = sub.add_parser("record-decision", help="record manual review feedback")
    decision.add_argument("--item-id", required=True)
    decision.add_argument("--decision", required=True, choices=DECISIONS)
    decision.add_argument("--reason", default=None)
    decision.set_defaults(func=cmd_record_decision)

    notify = sub.add_parser("notify-test", help="send a Telegram test message")
    notify.set_defaults(func=cmd_notify_test)

    health = sub.add_parser("health", help="container health check")
    health.set_defaults(func=cmd_health)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "health":
        return cmd_health()
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(
        config.settings.log_level if args.command in ("run", "once") else "WARNING"
    )
    try:
        return int(args.func(config, args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except EbayAuthError as exc:
        print(f"eBay authentication error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
