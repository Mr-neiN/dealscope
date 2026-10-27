"""Quality and operating-cost reporting.

Alert volume alone is not a quality metric. Decisions feed back into
precision, while API usage and coverage show whether the scanner is running
sustainably.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from src.config import AppConfig
from src.discovery.queries import SchedulePlan
from src.storage.repository import Repository

DECISION_LABELS = {
    "good_deal": "genuine underpriced item",
    "purchased": "bought",
    "wrong_product": "wrong product match",
    "defective": "defective or incomplete",
    "unavailable": "not available at the shown total",
    "too_risky": "too risky",
    "not_needed": "good price but not needed",
}


def generate_report(
    repository: Repository,
    config: AppConfig,
    *,
    now: datetime | None = None,
    days: int = 7,
    plan: SchedulePlan | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_text = _iso(since)
    today = now.strftime("%Y-%m-%d")
    connection = repository.database.connection

    def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
        row = connection.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    new_listings = scalar(
        "SELECT COUNT(*) FROM listings WHERE first_seen_at>=?", (since_text,)
    )
    listings_by_status = {
        str(row["status"]): int(row["total"])
        for row in connection.execute(
            "SELECT status, COUNT(*) AS total FROM listings GROUP BY status"
        ).fetchall()
    }
    observations = scalar(
        "SELECT COUNT(*) FROM listing_observations WHERE observed_at>=?", (since_text,)
    )
    alert_rows = connection.execute(
        "SELECT state, COUNT(*) AS total, SUM(CASE WHEN kind='auction_ending' THEN 1 ELSE 0 END) AS auctions "
        "FROM alerts WHERE created_at>=? GROUP BY state",
        (since_text,),
    ).fetchall()
    alerts_by_state = {str(row["state"]): int(row["total"]) for row in alert_rows}
    alert_kinds = {
        str(row["state"]): int(row["auctions"] or 0) for row in alert_rows
    }
    delivered_total = alerts_by_state.get("delivered", 0)

    decision_rows = connection.execute(
        "SELECT decision, COUNT(*) AS total FROM user_decisions "
        "WHERE created_at>=? GROUP BY decision",
        (since_text,),
    ).fetchall()
    decisions = {str(row["decision"]): int(row["total"]) for row in decision_rows}
    good = decisions.get("good_deal", 0) + decisions.get("purchased", 0)
    bad = sum(
        decisions.get(key, 0)
        for key in ("wrong_product", "defective", "unavailable", "too_risky")
    )
    reviewed = good + bad
    precision = (good / reviewed) if reviewed else None

    delay_row = connection.execute(
        """
        SELECT AVG((julianday(a.created_at) - julianday(l.first_seen_at)) * 24.0) AS hours
        FROM alerts a JOIN listings l ON l.item_id=a.listing_id
        WHERE a.created_at>=? AND a.state='delivered'
        """,
        (since_text,),
    ).fetchone()
    avg_alert_delay_hours = (
        round(float(delay_row["hours"]), 2)
        if delay_row and delay_row["hours"] is not None
        else None
    )

    run_rows = connection.execute(
        "SELECT outcome, COUNT(*) AS total FROM search_runs "
        "WHERE started_at>=? GROUP BY outcome",
        (since_text,),
    ).fetchall()
    runs_by_outcome = {str(row["outcome"]): int(row["total"]) for row in run_rows}

    api_rows = connection.execute(
        "SELECT endpoint, SUM(calls) AS total FROM api_calls WHERE day>=? GROUP BY endpoint",
        (since_text[:10],),
    ).fetchall()
    api_by_endpoint = {
        str(row["endpoint"]): int(row["total"] or 0) for row in api_rows
    }
    calls_since = sum(api_by_endpoint.values())
    calls_today = repository.calls_today(today)
    calls_per_delivered = (
        round(calls_since / delivered_total, 1) if delivered_total else None
    )
    calls_per_good = round(calls_since / good, 1) if good else None

    coverage_rows = connection.execute(
        "SELECT job_key, interval_seconds, last_success_at, last_error, "
        "consecutive_failures FROM job_state ORDER BY job_key"
    ).fetchall()
    stale_jobs: list[dict[str, Any]] = []
    for row in coverage_rows:
        last_success = _parse_iso(row["last_success_at"])
        if last_success is None:
            stale = True
            age_hours = None
        else:
            age_hours = round((now - last_success).total_seconds() / 3600, 1)
            stale = age_hours > max(2.5 * int(row["interval_seconds"]) / 3600, 2)
        if stale:
            stale_jobs.append(
                {
                    "job": row["job_key"],
                    "last_success_at": row["last_success_at"],
                    "hours_since_success": age_hours,
                    "consecutive_failures": int(row["consecutive_failures"] or 0),
                    "last_error": row["last_error"],
                }
            )

    db_size = (
        repository.database.path.stat().st_size
        if repository.database.path.exists()
        else 0
    )
    return {
        "generated_at": _iso(now),
        "window_days": days,
        "listings": {
            "new_in_window": new_listings,
            "by_status": listings_by_status,
            "observations_in_window": observations,
        },
        "alerts": {
            "by_state": alerts_by_state,
            "auction_alerts_by_state": alert_kinds,
            "average_delay_hours": avg_alert_delay_hours,
            "daily_cap": config.alerts.max_per_day,
        },
        "decisions": {
            "counts": decisions,
            "labels": {key: DECISION_LABELS.get(key, key) for key in decisions},
            "precision": round(precision, 3) if precision is not None else None,
            "good": good,
            "reviewed": reviewed,
        },
        "api": {
            "calls_in_window": calls_since,
            "calls_today": calls_today,
            "daily_budget": config.effective_daily_budget,
            "by_endpoint": api_by_endpoint,
            "calls_per_delivered_alert": calls_per_delivered,
            "calls_per_good_decision": calls_per_good,
            "planned_calls_per_day": plan.planned_calls_per_day if plan else None,
            "schedule_scale_factor": plan.scale_factor if plan else None,
        },
        "search": {"runs_by_outcome": runs_by_outcome},
        "coverage": {
            "jobs": len(coverage_rows),
            "stale_jobs": stale_jobs,
        },
        "storage": {"database_bytes": db_size},
    }


def render_report(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    generated = report.get("generated_at", "")
    window = report.get("window_days", 7)
    lines.append(f"eBay scanner report - last {window} days ({generated})")
    lines.append("")
    listings = report.get("listings", {})
    lines.append(
        f"Listings: {listings.get('new_in_window', 0)} new, "
        f"{listings.get('observations_in_window', 0)} price/status observations"
    )
    status = listings.get("by_status", {})
    if status:
        lines.append(
            "  status: " + ", ".join(f"{key}={value}" for key, value in status.items())
        )
    alerts = report.get("alerts", {})
    states = alerts.get("by_state", {})
    lines.append(
        "Alerts: "
        + (", ".join(f"{key}={value}" for key, value in states.items()) or "none")
        + f"; daily cap {alerts.get('daily_cap')}"
    )
    if alerts.get("average_delay_hours") is not None:
        lines.append(f"  average first-seen -> alert delay: {alerts['average_delay_hours']} h")
    decisions = report.get("decisions", {})
    lines.append(
        f"Reviewed: {decisions.get('reviewed', 0)}; "
        f"precision: {decisions.get('precision') if decisions.get('precision') is not None else 'n/a'}"
    )
    for key, count in decisions.get("counts", {}).items():
        label = decisions.get("labels", {}).get(key, key)
        lines.append(f"  {key}: {count} ({label})")
    api = report.get("api", {})
    lines.append(
        f"API: {api.get('calls_today', 0)}/{api.get('daily_budget', '?')} calls today; "
        f"{api.get('calls_in_window', 0)} in window; "
        f"{api.get('calls_per_delivered_alert') or 'n/a'} calls/delivered alert"
    )
    if api.get("planned_calls_per_day") is not None:
        lines.append(
            f"  planned: {api['planned_calls_per_day']} calls/day, "
            f"schedule stretch {api.get('schedule_scale_factor')}x"
        )
    if api.get("by_endpoint"):
        lines.append(
            "  by endpoint: "
            + ", ".join(f"{key}={value}" for key, value in api["by_endpoint"].items())
        )
    search = report.get("search", {}).get("runs_by_outcome", {})
    if search:
        lines.append(
            "Search runs: " + ", ".join(f"{key}={value}" for key, value in search.items())
        )
    coverage = report.get("coverage", {})
    stale = coverage.get("stale_jobs", [])
    lines.append(f"Coverage: {coverage.get('jobs', 0)} jobs, {len(stale)} stale")
    for job in stale[:5]:
        lines.append(
            f"  stale: {job['job']} (last success {job.get('last_success_at') or 'never'}, "
            f"failures {job.get('consecutive_failures', 0)})"
        )
    lines.append(f"Database size: {report.get('storage', {}).get('database_bytes', 0)} bytes")
    return "\n".join(lines)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
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
