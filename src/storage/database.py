"""SQLite initialization and migration helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    priority TEXT NOT NULL,
    condition_class TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    item_id TEXT PRIMARY KEY,
    marketplace TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    url TEXT NOT NULL,
    price_minor INTEGER,
    currency TEXT,
    price_eur_minor INTEGER,
    shipping_minor INTEGER,
    shipping_currency TEXT,
    shipping_eur_minor INTEGER,
    shipping_known INTEGER NOT NULL DEFAULT 0,
    ship_to_ok INTEGER,
    import_charges_eur_minor INTEGER,
    condition_label TEXT,
    condition_class TEXT NOT NULL DEFAULT 'unknown',
    buying_option TEXT NOT NULL DEFAULT 'fixed',
    bid_count INTEGER,
    current_bid_eur_minor INTEGER,
    item_end_at TEXT,
    seller_name TEXT,
    seller_feedback_pct REAL,
    seller_feedback_score INTEGER,
    location_country TEXT,
    location_city TEXT,
    image_count INTEGER NOT NULL DEFAULT 0,
    product_id TEXT,
    match_confidence REAL,
    listing_kind TEXT NOT NULL DEFAULT 'unknown',
    risk_level TEXT NOT NULL DEFAULT 'medium',
    risk_flags_json TEXT NOT NULL DEFAULT '[]',
    description_excerpt TEXT,
    aspects_json TEXT NOT NULL DEFAULT '{}',
    detail_fetched_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_alerted_at TEXT,
    total_cost_eur_minor INTEGER,
    value_eur_minor INTEGER,
    value_source_kind TEXT,
    value_source TEXT,
    valuation_label TEXT,
    valuation_score REAL,
    valuation_sample_size INTEGER,
    discount_pct REAL,
    savings_eur_minor INTEGER,
    max_bid_eur_minor INTEGER,
    deal_score REAL,
    confidence_score REAL,
    completeness REAL,
    evaluated_at TEXT,
    evaluation_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_product ON listings(product_id, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price_eur_minor);

CREATE TABLE IF NOT EXISTS listing_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES listings(item_id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    price_minor INTEGER,
    currency TEXT,
    price_eur_minor INTEGER,
    shipping_eur_minor INTEGER,
    shipping_known INTEGER NOT NULL DEFAULT 0,
    condition_class TEXT,
    buying_option TEXT,
    bid_count INTEGER,
    current_bid_eur_minor INTEGER,
    item_end_at TEXT,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    UNIQUE(item_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_observations_item ON listing_observations(item_id, observed_at);

CREATE TABLE IF NOT EXISTS search_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    strategy TEXT NOT NULL,
    query TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    product_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    http_status INTEGER,
    items_returned INTEGER NOT NULL DEFAULT 0,
    new_items INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON search_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_job ON search_runs(job_key, started_at);

CREATE TABLE IF NOT EXISTS job_state (
    job_key TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    query TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    priority TEXT NOT NULL,
    last_started_at TEXT,
    last_finished_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS price_comparables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    condition_class TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    price_eur_minor INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    sample_size INTEGER NOT NULL DEFAULT 1,
    source_ref TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comparables_product
    ON price_comparables(product_id, condition_class, observed_at);

CREATE TABLE IF NOT EXISTS market_estimates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listings(item_id) ON DELETE CASCADE,
    product_id TEXT,
    condition_class TEXT,
    value_eur_minor INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    source TEXT NOT NULL,
    sample_size INTEGER NOT NULL DEFAULT 0,
    window_days INTEGER,
    outliers_removed INTEGER NOT NULL DEFAULT 0,
    confidence_score REAL NOT NULL,
    confidence_label TEXT NOT NULL,
    method TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_estimates_listing ON market_estimates(listing_id, created_at);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listings(item_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    alert_key TEXT NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    telegram_message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(listing_id, alert_key)
);
CREATE INDEX IF NOT EXISTS idx_alerts_state ON alerts(state, next_attempt_at);

CREATE TABLE IF NOT EXISTS user_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(listing_id, decision)
);

CREATE TABLE IF NOT EXISTS api_calls (
    day TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    marketplace TEXT NOT NULL DEFAULT '',
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, endpoint, marketplace)
);

CREATE TABLE IF NOT EXISTS taxonomy_categories (
    marketplace TEXT NOT NULL,
    hint TEXT NOT NULL,
    category_id TEXT NOT NULL,
    category_name TEXT,
    tree_id TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (marketplace, hint)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    day TEXT NOT NULL,
    currency TEXT NOT NULL,
    rate_per_eur TEXT NOT NULL,
    PRIMARY KEY (day, currency)
);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self.path),
                timeout=10.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=5000")
            self._connection = connection
        return self._connection

    def initialize(self) -> None:
        connection = self.connection
        current = self._schema_version(connection)
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {current} is newer than this application "
                f"supports ({SCHEMA_VERSION}); upgrade the container image"
            )
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    @staticmethod
    def _schema_version(connection: sqlite3.Connection) -> int:
        try:
            row = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

    def seed_products(self, products: list[dict[str, str]]) -> None:
        """Store a cache of catalogue identities for joins and reporting."""
        connection = self.connection
        with self.transaction():
            for product in products:
                connection.execute(
                    """
                    INSERT INTO products(id, name, category, priority, condition_class,
                                         aliases_json, spec_json, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        category=excluded.category,
                        priority=excluded.priority,
                        condition_class=excluded.condition_class,
                        aliases_json=excluded.aliases_json,
                        spec_json=excluded.spec_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        product["id"],
                        product["name"],
                        product["category"],
                        product["priority"],
                        product["condition_class"],
                        product["aliases_json"],
                        product["spec_json"],
                        product["updated_at"],
                    ),
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(destination))
        try:
            self.connection.backup(target)
        finally:
            target.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
