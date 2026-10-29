"""Daily Telegram digest of what the scanner found.

Live alerts fire per listing, but in shadow mode - or on a slow day -
Telegram stays completely silent, so the operator only sees progress by
opening the dashboard. The digest is one compact summary per day: the best
candidates found in the last 24h, alert delivery health, and early precision
signals from manual decisions. It is a recap, not an extra alert channel.
"""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.config import AppConfig
from src.money import format_eur
from src.storage.repository import utc_iso

BAD_DECISIONS = ("wrong_product", "defective", "unavailable", "too_risky")


def digest_due(
    now: datetime,
    sent_day: date | None,
    target_hour: int,
    tz_name: str,
) -> date | None:
    """Local day to send the digest for, or ``None`` when it is not due.

    Due once the local clock has passed ``target_hour`` and no digest for
    that local day has gone out. After a downtime the scanner sends one
    catch-up digest for the current day, never a backlog.
    """
    tz = _tz(tz_name)
    day = now.astimezone(tz).date()
    if sent_day == day:
        return None
    if now.astimezone(tz).hour < target_hour:
        return None
    return day


def build_digest(
    repository: Any,
    config: AppConfig,
    now: datetime,
    *,
    top: int = 5,
) -> str:
    """Render the daily digest as Telegram HTML."""
    connection = repository.database.connection
    since = utc_iso(now - timedelta(hours=24))

    alert_rows = connection.execute(
        "SELECT state, COUNT(*) AS total FROM alerts "
        "WHERE created_at>=? GROUP BY state",
        (since,),
    ).fetchall()
    alerts = {str(row["state"]): int(row["total"]) for row in alert_rows}

    candidates = connection.execute(
        """
        SELECT l.item_id, l.title, l.url, l.deal_score, l.savings_eur_minor,
               l.total_cost_eur_minor, l.buying_option, p.name AS product
        FROM listings l
        LEFT JOIN products p ON p.id = l.product_id
        WHERE l.status='active' AND l.deal_score IS NOT NULL AND l.first_seen_at>=?
        ORDER BY l.deal_score DESC, l.savings_eur_minor DESC
        LIMIT ?
        """,
        (since, max(1, top)),
    ).fetchall()

    since7 = utc_iso(now - timedelta(days=7))
    decision_rows = connection.execute(
        "SELECT decision, COUNT(*) AS total FROM user_decisions "
        "WHERE created_at>=? GROUP BY decision",
        (since7,),
    ).fetchall()
    decisions = {str(row["decision"]): int(row["total"]) for row in decision_rows}
    good = decisions.get("good_deal", 0) + decisions.get("purchased", 0)
    rejected = sum(decisions.get(key, 0) for key in BAD_DECISIONS)
    reviewed = good + rejected
    precision = round(good / reviewed) if reviewed else None

    mode = "shadow mode: candidates only, no live alerts" if config.settings.shadow_mode else "live"
    day = _tz(config.settings.timezone)
    heading = now.astimezone(day).strftime("%d %b %Y")
    lines = [f"<b>Daily digest - {heading} ({mode})</b>", ""]

    if alerts:
        parts = [
            f"{name} {alerts[name]}"
            for name in ("delivered", "pending", "failed", "shadow")
            if alerts.get(name)
        ]
        lines.append("<b>Alerts (24h):</b> " + ", ".join(parts))
    if candidates:
        lines.append("")
        lines.append("<b>Top candidates (24h):</b>")
        for index, row in enumerate(candidates, start=1):
            title = html.escape(str(row["title"] or row["item_id"]))
            url = html.escape(str(row["url"] or ""), quote=True)
            line = (
                f"{index}. <a href=\"{url}\">{title}</a>"
                f" - score {row['deal_score']:.0f}"
            )
            if row["total_cost_eur_minor"] is not None:
                line += f", total {format_eur(int(row['total_cost_eur_minor']))}"
            if row["savings_eur_minor"] is not None and int(row["savings_eur_minor"]) > 0:
                line += f", save {format_eur(int(row['savings_eur_minor']))}"
            lines.append(line)
    else:
        lines.append("No scored candidates in the last 24h.")

    if reviewed:
        lines.append("")
        lines.append(
            f"Review (7d): {good} positive / {rejected} rejected"
            + (f" - precision {precision}%" if precision is not None else "")
        )

    return "\n".join(lines)


def _tz(name: str) -> timezone | Any:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone.utc
