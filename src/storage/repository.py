"""Database access for listings, observations, estimates, alerts and quota."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from src.ebay.models import ListingItem
from src.storage.database import Database


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class IngestedListing:
    item: ListingItem
    normalized_title: str
    condition_class: str
    buying_option: str
    price_eur_minor: int | None
    shipping_eur_minor: int | None
    import_charges_eur_minor: int | None
    current_bid_eur_minor: int | None


@dataclass(frozen=True)
class ListingRecord:
    item_id: str
    marketplace: str
    title: str
    normalized_title: str
    url: str
    price_minor: int | None
    currency: str | None
    price_eur_minor: int | None
    shipping_eur_minor: int | None
    shipping_known: bool
    ship_to_ok: bool | None
    import_charges_eur_minor: int | None
    condition_label: str | None
    condition_class: str
    buying_option: str
    bid_count: int | None
    current_bid_eur_minor: int | None
    item_end_at: str | None
    seller_name: str | None
    seller_feedback_pct: float | None
    seller_feedback_score: int | None
    location_country: str | None
    location_city: str | None
    image_count: int
    product_id: str | None
    match_confidence: float | None
    listing_kind: str
    risk_level: str
    risk_flags: tuple[str, ...]
    description_excerpt: str | None
    aspects: Mapping[str, tuple[str, ...]]
    detail_fetched_at: str | None
    status: str
    first_seen_at: str
    last_seen_at: str
    last_alerted_at: str | None
    total_cost_eur_minor: int | None
    value_eur_minor: int | None
    value_source_kind: str | None
    value_source: str | None
    valuation_label: str | None
    valuation_score: float | None
    valuation_sample_size: int | None
    discount_pct: float | None
    savings_eur_minor: int | None
    max_bid_eur_minor: int | None
    deal_score: float | None
    confidence_score: float | None
    completeness: float | None
    evaluated_at: str | None

    @property
    def risk_flag_set(self) -> set[str]:
        return set(self.risk_flags)


@dataclass(frozen=True)
class UpsertResult:
    listing: ListingRecord
    created: bool
    changed: bool
    changed_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlertRecord:
    id: int
    listing_id: str
    kind: str
    alert_key: str
    state: str
    priority: int
    payload: Mapping[str, Any]
    attempts: int
    next_attempt_at: str | None
    last_error: str | None
    created_at: str
    delivered_at: str | None


@dataclass(frozen=True)
class RunRecord:
    job_key: str
    strategy: str
    query: str
    marketplace: str
    product_id: str | None
    started_at: datetime


class Repository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def close(self) -> None:
        self.database.close()

    # -------------------------------------------------------------- catalogue
    def seed_products(self, products: Iterable[Mapping[str, Any]], now: datetime | None = None) -> None:
        timestamp = utc_iso(now or utc_now())
        payload = [
            {
                "id": product["id"],
                "name": product["name"],
                "category": product["category"],
                "priority": product["priority"],
                "condition_class": product["condition_class"],
                "aliases_json": json.dumps(list(product.get("aliases", [])), ensure_ascii=False),
                "spec_json": json.dumps(dict(product.get("spec", {})), ensure_ascii=False),
                "updated_at": timestamp,
            }
            for product in products
        ]
        self.database.seed_products(payload)

    # ----------------------------------------------------------------- quota
    def record_api_call(self, day: str, endpoint: str, marketplace: str = "") -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO api_calls(day, endpoint, marketplace, calls)
                VALUES(?, ?, ?, 1)
                ON CONFLICT(day, endpoint, marketplace)
                DO UPDATE SET calls=calls + 1
                """,
                (day, endpoint, marketplace or ""),
            )

    def calls_today(self, day: str, endpoint: str | None = None) -> int:
        if endpoint is None:
            row = self.database.connection.execute(
                "SELECT COALESCE(SUM(calls), 0) AS total FROM api_calls WHERE day=?",
                (day,),
            ).fetchone()
        else:
            row = self.database.connection.execute(
                "SELECT COALESCE(SUM(calls), 0) AS total FROM api_calls WHERE day=? AND endpoint=?",
                (day, endpoint),
            ).fetchone()
        return int(row["total"]) if row else 0

    # ------------------------------------------------------------------ jobs
    def upsert_job(
        self,
        job_key: str,
        strategy: str,
        query: str,
        marketplace: str,
        interval_seconds: int,
        priority: str,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO job_state(job_key, strategy, query, marketplace,
                                      interval_seconds, priority)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_key) DO UPDATE SET
                    strategy=excluded.strategy,
                    query=excluded.query,
                    marketplace=excluded.marketplace,
                    interval_seconds=excluded.interval_seconds,
                    priority=excluded.priority
                """,
                (job_key, strategy, query, marketplace, interval_seconds, priority),
            )

    def get_job_states(self) -> dict[str, Mapping[str, Any]]:
        rows = self.database.connection.execute("SELECT * FROM job_state").fetchall()
        return {row["job_key"]: dict(row) for row in rows}

    def acquire_job(self, job_key: str, now: datetime, lock_seconds: int = 900) -> bool:
        cutoff = utc_iso(
            datetime.fromtimestamp(now.timestamp() - lock_seconds, tz=timezone.utc)
        )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE job_state
                SET last_started_at=?
                WHERE job_key=?
                  AND (last_started_at IS NULL OR last_started_at < ?)
                """,
                (utc_iso(now), job_key, cutoff),
            )
            return cursor.rowcount == 1

    def finish_job(
        self,
        job_key: str,
        *,
        now: datetime,
        success: bool,
        error: str | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            if success:
                connection.execute(
                    """
                    UPDATE job_state
                    SET last_finished_at=?, last_success_at=?, last_error=NULL,
                        consecutive_failures=0
                    WHERE job_key=?
                    """,
                    (utc_iso(now), utc_iso(now), job_key),
                )
            else:
                connection.execute(
                    """
                    UPDATE job_state
                    SET last_finished_at=?, last_error=?,
                        consecutive_failures=consecutive_failures + 1
                    WHERE job_key=?
                    """,
                    (utc_iso(now), (error or "unknown error")[:500], job_key),
                )

    def start_search_run(
        self,
        *,
        job_key: str,
        strategy: str,
        query: str,
        marketplace: str,
        product_id: str | None,
        now: datetime,
    ) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO search_runs(job_key, strategy, query, marketplace,
                                        product_id, started_at, outcome)
                VALUES(?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    job_key,
                    strategy,
                    query,
                    marketplace,
                    product_id,
                    utc_iso(now),
                ),
            )
            return int(cursor.lastrowid)

    def finish_search_run(
        self,
        run_id: int,
        *,
        now: datetime,
        outcome: str,
        http_status: int | None = None,
        items_returned: int = 0,
        new_items: int = 0,
        pages: int = 0,
        error: str | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE search_runs
                SET finished_at=?, outcome=?, http_status=?, items_returned=?,
                    new_items=?, pages=?, error=?
                WHERE id=?
                """,
                (
                    utc_iso(now),
                    outcome,
                    http_status,
                    items_returned,
                    new_items,
                    pages,
                    (error or "")[:500] or None,
                    run_id,
                ),
            )

    # -------------------------------------------------------------- ingestion
    def ingest_listing(self, ingested: IngestedListing, now: datetime) -> UpsertResult:
        item = ingested.item
        shipping = item.shipping
        shipping_minor = shipping.cost.amount_minor if shipping.cost else None
        shipping_currency = shipping.cost.currency if shipping.cost else None
        existing_row = self.database.connection.execute(
            "SELECT * FROM listings WHERE item_id=?", (item.item_id,)
        ).fetchone()
        now_text = utc_iso(now)
        values = {
            "item_id": item.item_id,
            "marketplace": item.marketplace,
            "title": item.title,
            "normalized_title": ingested.normalized_title,
            "url": item.url,
            "price_minor": item.price.amount_minor,
            "currency": item.price.currency,
            "price_eur_minor": ingested.price_eur_minor,
            "shipping_minor": shipping_minor,
            "shipping_currency": shipping_currency,
            "shipping_eur_minor": ingested.shipping_eur_minor,
            "shipping_known": 1 if shipping.cost is not None else 0,
            "ship_to_ok": (
                None if shipping.ship_to_ok is None else (1 if shipping.ship_to_ok else 0)
            ),
            "import_charges_eur_minor": ingested.import_charges_eur_minor,
            "condition_label": item.condition_label,
            "condition_class": ingested.condition_class,
            "buying_option": ingested.buying_option,
            "bid_count": item.bid_count,
            "current_bid_eur_minor": ingested.current_bid_eur_minor,
            "item_end_at": utc_iso(item.item_end_at) if item.item_end_at else None,
            "seller_name": item.seller_name,
            "seller_feedback_pct": item.seller_feedback_pct,
            "seller_feedback_score": item.seller_feedback_score,
            "location_country": item.location_country,
            "location_city": item.location_city,
            "image_count": item.image_count,
        }

        with self.database.transaction() as connection:
            if existing_row is None:
                values.update(
                    {
                        "status": "active",
                        "first_seen_at": now_text,
                        "last_seen_at": now_text,
                        "last_checked_at": now_text,
                    }
                )
                columns = ", ".join(values)
                placeholders = ", ".join("?" for _ in values)
                connection.execute(
                    f"INSERT INTO listings({columns}) VALUES({placeholders})",
                    tuple(values.values()),
                )
                self._insert_observation(connection, item, ingested, now, "first_seen")
                created = True
                changed = True
                changed_fields: tuple[str, ...] = ("created",)
            else:
                previous = dict(existing_row)
                changed_fields_list: list[str] = []
                material_fields = (
                    "price_eur_minor",
                    "shipping_eur_minor",
                    "shipping_known",
                    "condition_class",
                    "buying_option",
                    "bid_count",
                    "current_bid_eur_minor",
                    "item_end_at",
                    "ship_to_ok",
                    "title",
                )
                for key in material_fields:
                    if previous.get(key) != values.get(key):
                        changed_fields_list.append(key)
                assignments = ", ".join(f"{key}=?" for key in values)
                connection.execute(
                    f"UPDATE listings SET {assignments}, status='active', last_seen_at=?, "
                    "last_checked_at=? WHERE item_id=?",
                    tuple(values.values()) + (now_text, now_text, item.item_id),
                )
                changed_fields = tuple(changed_fields_list)
                changed = bool(changed_fields)
                if changed:
                    reason = "changed:" + ",".join(changed_fields)
                    status = "active"
                    self._insert_observation(connection, item, ingested, now, reason, status)
                created = False

        record = self.get_listing(item.item_id)
        if record is None:  # pragma: no cover - guarded by the transaction above
            raise RuntimeError(f"listing {item.item_id} disappeared after upsert")
        return UpsertResult(record, created, changed, changed_fields)

    @staticmethod
    def _insert_observation(
        connection: Any,
        item: ListingItem,
        ingested: IngestedListing,
        now: datetime,
        reason: str,
        status: str = "active",
    ) -> None:
        import sqlite3

        try:
            connection.execute(
                """
                INSERT INTO listing_observations(
                    item_id, observed_at, price_minor, currency, price_eur_minor,
                    shipping_eur_minor, shipping_known, condition_class, buying_option,
                    bid_count, current_bid_eur_minor, item_end_at, status, reason)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.item_id,
                    utc_iso(now),
                    item.price.amount_minor,
                    item.price.currency,
                    ingested.price_eur_minor,
                    ingested.shipping_eur_minor,
                    1 if item.shipping.cost is not None else 0,
                    ingested.condition_class,
                    ingested.buying_option,
                    item.bid_count,
                    ingested.current_bid_eur_minor,
                    utc_iso(item.item_end_at) if item.item_end_at else None,
                    status,
                    reason,
                ),
            )
        except sqlite3.IntegrityError:
            # Two observations in the same millisecond carry no new information.
            pass

    def get_listing(self, item_id: str) -> ListingRecord | None:
        row = self.database.connection.execute(
            "SELECT * FROM listings WHERE item_id=?", (item_id,)
        ).fetchone()
        if row is None:
            return None
        return _listing_from_row(row)

    def save_classification(
        self,
        item_id: str,
        *,
        product_id: str | None,
        match_confidence: float | None,
        listing_kind: str,
        risk_level: str,
        risk_flags: Sequence[str],
        now: datetime,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE listings
                SET product_id=?, match_confidence=?, listing_kind=?, risk_level=?,
                    risk_flags_json=?, last_checked_at=?
                WHERE item_id=?
                """,
                (
                    product_id,
                    match_confidence,
                    listing_kind,
                    risk_level,
                    json.dumps(list(risk_flags), ensure_ascii=False),
                    utc_iso(now),
                    item_id,
                ),
            )

    def save_detail(
        self,
        item_id: str,
        *,
        description_excerpt: str | None,
        aspects: Mapping[str, Sequence[str]],
        shipping_eur_minor: int | None,
        shipping_known: bool,
        ship_to_ok: bool | None,
        import_charges_eur_minor: int | None,
        now: datetime,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE listings
                SET description_excerpt=?, aspects_json=?, shipping_eur_minor=?,
                    shipping_known=?, ship_to_ok=?, import_charges_eur_minor=?,
                    detail_fetched_at=?
                WHERE item_id=?
                """,
                (
                    description_excerpt[:4000] if description_excerpt else None,
                    json.dumps(
                        {key: list(value) for key, value in aspects.items()},
                        ensure_ascii=False,
                    ),
                    shipping_eur_minor,
                    1 if shipping_known else 0,
                    None if ship_to_ok is None else (1 if ship_to_ok else 0),
                    import_charges_eur_minor,
                    utc_iso(now),
                    item_id,
                ),
            )

    def save_evaluation(
        self,
        item_id: str,
        *,
        product_id: str | None,
        condition_class: str,
        value_eur_minor: int,
        source_kind: str,
        source: str,
        sample_size: int,
        window_days: int,
        outliers_removed: int,
        valuation_label: str,
        valuation_score: float,
        method: str,
        total_cost_eur_minor: int | None,
        discount_pct: float,
        savings_eur_minor: int,
        max_bid_eur_minor: int | None,
        deal_score: float,
        confidence_score: float,
        completeness: float,
        evaluation_payload: Mapping[str, Any],
        now: datetime,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO market_estimates(
                    listing_id, product_id, condition_class, value_eur_minor,
                    source_kind, source, sample_size, window_days, outliers_removed,
                    confidence_score, confidence_label, method, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    product_id,
                    condition_class,
                    value_eur_minor,
                    source_kind,
                    source,
                    sample_size,
                    window_days,
                    outliers_removed,
                    confidence_score,
                    valuation_label,
                    method,
                    utc_iso(now),
                ),
            )
            connection.execute(
                """
                UPDATE listings
                SET value_eur_minor=?, value_source_kind=?, value_source=?,
                    valuation_label=?, valuation_score=?, valuation_sample_size=?,
                    total_cost_eur_minor=?, discount_pct=?, savings_eur_minor=?,
                    max_bid_eur_minor=?, deal_score=?, confidence_score=?,
                    completeness=?, evaluated_at=?, evaluation_json=?
                WHERE item_id=?
                """,
                (
                    value_eur_minor,
                    source_kind,
                    source,
                    valuation_label,
                    valuation_score,
                    sample_size,
                    total_cost_eur_minor,
                    discount_pct,
                    savings_eur_minor,
                    max_bid_eur_minor,
                    deal_score,
                    confidence_score,
                    completeness,
                    utc_iso(now),
                    json.dumps(dict(evaluation_payload), ensure_ascii=False),
                    item_id,
                ),
            )

    # ------------------------------------------------------------ comparables
    def add_comparable(
        self,
        *,
        product_id: str,
        condition_class: str,
        price_eur_minor: int,
        observed_at: datetime,
        source_kind: str,
        source_ref: str,
        verified: bool,
        sample_size: int = 1,
        note: str | None = None,
        now: datetime | None = None,
    ) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO price_comparables(
                    product_id, condition_class, source_kind, price_eur_minor,
                    observed_at, sample_size, source_ref, verified, note, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    condition_class,
                    source_kind,
                    price_eur_minor,
                    utc_iso(observed_at),
                    sample_size,
                    source_ref,
                    1 if verified else 0,
                    note,
                    utc_iso(now or utc_now()),
                ),
            )
            return int(cursor.lastrowid)

    def get_comparables(
        self,
        product_id: str,
        condition_class: str,
        since: datetime,
    ) -> list[Mapping[str, Any]]:
        rows = self.database.connection.execute(
            """
            SELECT * FROM price_comparables
            WHERE product_id=? AND condition_class=? AND observed_at>=?
            ORDER BY observed_at DESC
            """,
            (product_id, condition_class, utc_iso(since)),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_active_listing_prices(
        self,
        product_id: str,
        condition_class: str,
        since: datetime,
        exclude_item_id: str,
        limit: int = 30,
    ) -> list[Mapping[str, Any]]:
        rows = self.database.connection.execute(
            """
            SELECT item_id, price_eur_minor, last_seen_at, title
            FROM listings
            WHERE product_id=? AND condition_class=? AND status='active'
              AND last_seen_at>=? AND item_id<>?
              AND listing_kind='complete'
              AND price_eur_minor IS NOT NULL
              AND risk_level IN ('low', 'medium')
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (
                product_id,
                condition_class,
                utc_iso(since),
                exclude_item_id,
                limit,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- alerts
    def create_alert(
        self,
        *,
        listing_id: str,
        kind: str,
        alert_key: str,
        state: str,
        payload: Mapping[str, Any],
        priority: int,
        now: datetime,
        next_attempt_at: datetime | None = None,
    ) -> int | None:
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM alerts WHERE listing_id=? AND alert_key=?",
                (listing_id, alert_key),
            ).fetchone()
            if existing is not None:
                return None
            cursor = connection.execute(
                """
                INSERT INTO alerts(listing_id, kind, alert_key, state, priority,
                                   payload_json, next_attempt_at, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    listing_id,
                    kind,
                    alert_key,
                    state,
                    priority,
                    json.dumps(dict(payload), ensure_ascii=False),
                    utc_iso(next_attempt_at) if next_attempt_at else None,
                    utc_iso(now),
                    utc_iso(now),
                ),
            )
            connection.execute(
                "UPDATE listings SET last_alerted_at=? WHERE item_id=?",
                (utc_iso(now), listing_id),
            )
            return int(cursor.lastrowid)

    def alerts_for_listing(self, listing_id: str) -> list[AlertRecord]:
        rows = self.database.connection.execute(
            "SELECT * FROM alerts WHERE listing_id=? ORDER BY created_at DESC",
            (listing_id,),
        ).fetchall()
        return [_alert_from_row(row) for row in rows]

    # The grace-period for 'sending' rows: a crashed deliverer must free the
    # alert for a later retry, but the window must be longer than any normal
    # Telegram round-trip so a slow-but-successful send is not double-owned.
    SENDING_GRACE_MINUTES = 5

    def claim_pending_alerts(self, now: datetime, *, limit: int = 10) -> list[AlertRecord]:
        """Atomically move due pending alerts to 'sending' (or reclaim stale
        ones) and return the claimed rows.

        The claim is a single UPDATE executed inside BEGIN IMMEDIATE, so two
        scanner processes can never both take the same alert and each send a
        Telegram message for the same deal.
        """
        now_iso = utc_iso(now)
        grace = utc_iso(
            now - timedelta(minutes=self.SENDING_GRACE_MINUTES)
        )
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                UPDATE alerts
                SET state='sending', updated_at=?,
                    attempts=CASE WHEN state='sending' THEN attempts+1
                                  ELSE attempts END
                WHERE id IN (
                    SELECT id FROM alerts
                    WHERE (state='pending'
                           AND (next_attempt_at IS NULL OR next_attempt_at<=?))
                       OR (state='sending' AND updated_at<=?)
                    ORDER BY priority DESC, created_at ASC
                    LIMIT ?
                )
                RETURNING *
                """,
                (now_iso, now_iso, grace, limit),
            ).fetchall()
        return [_alert_from_row(row) for row in rows]

    def pending_alerts_due(self, now: datetime, limit: int = 20) -> list[AlertRecord]:
        """For inspection only; delivery must go through claim_pending_alerts."""
        rows = self.database.connection.execute(
            """            SELECT * FROM alerts
            WHERE state='pending'
              AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (utc_iso(now), limit),
        ).fetchall()
        return [_alert_from_row(row) for row in rows]

    # ------------------------------------------------------------ runner lock
    LOCK_TTL_SECONDS = 120

    def acquire_ownership(
        self,
        owner_id: str,
        now: datetime | None = None,
        *,
        ttl_seconds: int = LOCK_TTL_SECONDS,
        owner_host: str = "",
    ) -> bool:
        """Claim, renew or (after ttl) take over the singleton scan lease.

        All decisions happen inside BEGIN IMMEDIATE, so concurrent claims
        from several containers are serialized by SQLite. Renewal re-enters
        when the row already belongs to ``owner_id``.
        """
        now = now or utc_now()
        now_iso = utc_iso(now)
        cutoff = utc_iso(now - timedelta(seconds=max(1, ttl_seconds)))
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT owner_id, owner_host, renewed_at FROM runner_lock WHERE id=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO runner_lock(id, owner_id, owner_host, renewed_at) "
                    "VALUES(1, ?, ?, ?)",
                    (owner_id, owner_host, now_iso),
                )
                return True
            if row["owner_id"] == owner_id:
                connection.execute(
                    "UPDATE runner_lock SET renewed_at=?, owner_host=? WHERE id=1",
                    (now_iso, owner_host or row["owner_host"]),
                )
                return True
            if str(row["renewed_at"]) < cutoff:
                connection.execute(
                    "UPDATE runner_lock SET owner_id=?, owner_host=?, renewed_at=? "
                    "WHERE id=1",
                    (owner_id, owner_host, now_iso),
                )
                return True
        return False

    def release_ownership(self, owner_id: str, now: datetime | None = None) -> None:
        now = now or utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM runner_lock WHERE id=1 AND owner_id=?",
                (owner_id,),
            )

    def owner_info(self) -> dict[str, str] | None:
        row = self.database.connection.execute(
            "SELECT owner_id, owner_host, renewed_at FROM runner_lock WHERE id=1"
        ).fetchone()
        if row is None:
            return None
        return {
            "owner_id": str(row["owner_id"]),
            "owner_host": str(row["owner_host"]),
            "renewed_at": str(row["renewed_at"]),
        }

    def mark_alert_delivered(
        self, alert_id: int, *, message_id: str | None, now: datetime
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE alerts
                SET state='delivered', delivered_at=?, updated_at=?,
                    telegram_message_id=?, last_error=NULL, next_attempt_at=NULL
                WHERE id=?
                """,
                (utc_iso(now), utc_iso(now), message_id, alert_id),
            )

    def mark_alert_failed(
        self,
        alert_id: int,
        *,
        error: str,
        next_attempt_at: datetime | None,
        now: datetime,
        permanent: bool = False,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE alerts
                SET state=?, attempts=attempts + 1, last_error=?, next_attempt_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    "failed" if permanent or next_attempt_at is None else "pending",
                    error[:500],
                    utc_iso(next_attempt_at) if next_attempt_at else None,
                    utc_iso(now),
                    alert_id,
                ),
            )

    def alert_count_for_day(self, day: str, *, include_shadow: bool = False) -> int:
        if include_shadow:
            row = self.database.connection.execute(
                "SELECT COUNT(*) AS total FROM alerts WHERE substr(created_at,1,10)=?",
                (day,),
            ).fetchone()
        else:
            row = self.database.connection.execute(
                "SELECT COUNT(*) AS total FROM alerts WHERE substr(created_at,1,10)=? "
                "AND state<>'shadow'",
                (day,),
            ).fetchone()
        return int(row["total"]) if row else 0

    # -------------------------------------------------------------- decisions
    def record_decision(
        self,
        *,
        listing_id: str,
        decision: str,
        reason: str | None,
        now: datetime | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO user_decisions(listing_id, decision, reason, created_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(listing_id, decision) DO UPDATE SET
                    reason=excluded.reason,
                    created_at=excluded.created_at
                """,
                (listing_id, decision, reason, utc_iso(now or utc_now())),
            )

    # ---------------------------------------------------------------- status
    def mark_stale(self, *, now: datetime, stale_hours: int, unknown_hours: int) -> tuple[int, int]:
        now_ts = now.timestamp()
        stale_cutoff = utc_iso(
            datetime.fromtimestamp(now_ts - stale_hours * 3600, tz=timezone.utc)
        )
        unknown_cutoff = utc_iso(
            datetime.fromtimestamp(now_ts - unknown_hours * 3600, tz=timezone.utc)
        )
        with self.database.transaction() as connection:
            stale = connection.execute(
                "UPDATE listings SET status='not_seen' WHERE status='active' AND last_seen_at<?",
                (stale_cutoff,),
            ).rowcount
            unknown = connection.execute(
                "UPDATE listings SET status='unknown' WHERE status IN ('active','not_seen') "
                "AND last_seen_at<?",
                (unknown_cutoff,),
            ).rowcount
        return stale, unknown

    def prune(self, *, now: datetime, observation_days: int, alert_days: int, api_call_days: int) -> dict[str, int]:
        now_ts = now.timestamp()

        def cutoff(days: int) -> str:
            return utc_iso(datetime.fromtimestamp(now_ts - days * 86400, tz=timezone.utc))

        with self.database.transaction() as connection:
            observations = connection.execute(
                "DELETE FROM listing_observations WHERE observed_at<?",
                (cutoff(observation_days),),
            ).rowcount
            alerts = connection.execute(
                "DELETE FROM alerts WHERE created_at<? AND state IN ('delivered','shadow','failed')",
                (cutoff(alert_days),),
            ).rowcount
            calls = connection.execute(
                "DELETE FROM api_calls WHERE day<?", (cutoff(api_call_days)[:10],)
            ).rowcount
            estimates = connection.execute(
                "DELETE FROM market_estimates WHERE created_at<?",
                (cutoff(alert_days),),
            ).rowcount
        return {
            "observations": observations,
            "alerts": alerts,
            "api_calls": calls,
            "market_estimates": estimates,
        }

    # -------------------------------------------------------------- taxonomy
    def get_taxonomy_category(
        self, marketplace: str, hint: str, *, max_age_days: int, now: datetime
    ) -> Mapping[str, Any] | None:
        row = self.database.connection.execute(
            "SELECT * FROM taxonomy_categories WHERE marketplace=? AND hint=?",
            (marketplace, hint),
        ).fetchone()
        if row is None:
            return None
        try:
            fetched = datetime.fromisoformat(str(row["fetched_at"]).replace("Z", "+00:00"))
        except ValueError:
            return None
        if (now - fetched).days > max_age_days:
            return None
        return dict(row)

    def save_taxonomy_category(
        self,
        *,
        marketplace: str,
        hint: str,
        category_id: str,
        category_name: str | None,
        tree_id: str | None,
        now: datetime,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO taxonomy_categories(marketplace, hint, category_id,
                                                category_name, tree_id, fetched_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(marketplace, hint) DO UPDATE SET
                    category_id=excluded.category_id,
                    category_name=excluded.category_name,
                    tree_id=excluded.tree_id,
                    fetched_at=excluded.fetched_at
                """,
                (marketplace, hint, category_id, category_name, tree_id, utc_iso(now)),
            )

    # ---------------------------------------------------------------- fx
    def latest_fx_rates(self) -> dict[str, str]:
        rows = self.database.connection.execute(
            """
            SELECT currency, rate_per_eur FROM fx_rates
            WHERE day=(SELECT MAX(day) FROM fx_rates)
            """
        ).fetchall()
        return {str(row["currency"]): str(row["rate_per_eur"]) for row in rows}

    def save_fx_rates(self, day: str, rates: Mapping[str, str]) -> None:
        with self.database.transaction() as connection:
            for currency, rate in rates.items():
                connection.execute(
                    """
                    INSERT INTO fx_rates(day, currency, rate_per_eur)
                    VALUES(?, ?, ?)
                    ON CONFLICT(day, currency) DO UPDATE SET rate_per_eur=excluded.rate_per_eur
                    """,
                    (day, currency, rate),
                )

    # -------------------------------------------------------------- listings
    def update_listing_status(self, item_id: str, status: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE listings SET status=? WHERE item_id=?", (status, item_id)
            )


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _listing_from_row(row: Any) -> ListingRecord:
    try:
        flags = tuple(json.loads(row["risk_flags_json"] or "[]"))
    except (ValueError, TypeError):
        flags = ()
    try:
        aspects_raw = json.loads(row["aspects_json"] or "{}")
        aspects = {
            str(key): tuple(str(entry) for entry in value)
            for key, value in aspects_raw.items()
            if isinstance(value, list)
        }
    except (ValueError, TypeError, AttributeError):
        aspects = {}
    return ListingRecord(
        item_id=row["item_id"],
        marketplace=row["marketplace"],
        title=row["title"],
        normalized_title=row["normalized_title"],
        url=row["url"],
        price_minor=row["price_minor"],
        currency=row["currency"],
        price_eur_minor=row["price_eur_minor"],
        shipping_eur_minor=row["shipping_eur_minor"],
        shipping_known=bool(row["shipping_known"]),
        ship_to_ok=_bool_or_none(row["ship_to_ok"]),
        import_charges_eur_minor=row["import_charges_eur_minor"],
        condition_label=row["condition_label"],
        condition_class=row["condition_class"],
        buying_option=row["buying_option"],
        bid_count=row["bid_count"],
        current_bid_eur_minor=row["current_bid_eur_minor"],
        item_end_at=row["item_end_at"],
        seller_name=row["seller_name"],
        seller_feedback_pct=row["seller_feedback_pct"],
        seller_feedback_score=row["seller_feedback_score"],
        location_country=row["location_country"],
        location_city=row["location_city"],
        image_count=int(row["image_count"] or 0),
        product_id=row["product_id"],
        match_confidence=row["match_confidence"],
        listing_kind=row["listing_kind"],
        risk_level=row["risk_level"],
        risk_flags=flags,
        description_excerpt=row["description_excerpt"],
        aspects=aspects,
        detail_fetched_at=row["detail_fetched_at"],
        status=row["status"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        last_alerted_at=row["last_alerted_at"],
        total_cost_eur_minor=row["total_cost_eur_minor"],
        value_eur_minor=row["value_eur_minor"],
        value_source_kind=row["value_source_kind"],
        value_source=row["value_source"],
        valuation_label=row["valuation_label"],
        valuation_score=row["valuation_score"],
        valuation_sample_size=row["valuation_sample_size"],
        discount_pct=row["discount_pct"],
        savings_eur_minor=row["savings_eur_minor"],
        max_bid_eur_minor=row["max_bid_eur_minor"],
        deal_score=row["deal_score"],
        confidence_score=row["confidence_score"],
        completeness=row["completeness"],
        evaluated_at=row["evaluated_at"],
    )


def _alert_from_row(row: Any) -> AlertRecord:
    try:
        payload = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        payload = {}
    return AlertRecord(
        id=row["id"],
        listing_id=row["listing_id"],
        kind=row["kind"],
        alert_key=row["alert_key"],
        state=row["state"],
        priority=row["priority"],
        payload=payload if isinstance(payload, dict) else {},
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        delivered_at=row["delivered_at"],
    )
