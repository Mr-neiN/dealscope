"""JSON API + single-page dashboard over the scanner's SQLite database.

Design notes:

- Stdlib ``http.server`` only, so the dashboard adds no dependencies and can
  run beside the scanner process in the same container image.
- Every HTTP request opens its own ``sqlite3`` connection (read-only for
  queries). This keeps the dashboard safe under ``ThreadingHTTPServer`` and
  lets it run in a separate container sharing the ``./data`` volume: SQLite
  WAL mode supports many concurrent readers.
- The write endpoints are the manual decision record (reusing
  ``Repository.record_decision`` exactly like the CLI does) and the manual
  scan/Telegram-test endpoints in :mod:`src.gui.scanner_actions`, which
  reuse the scanner's job claims and API budget ledger.
- Listing text comes from eBay; responses always escape it client-side by
  inserting it via ``textContent``/``document.createElement``, never HTML.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from src.config import DECISIONS
from src.gui.page import PAGE_HTML
from src.gui.scanner_actions import ScanBusy, ScanCooldown
from src.storage.database import Database
from src.storage.repository import Repository, utc_iso

log = logging.getLogger(__name__)

MAX_LIMIT = 500
DEFAULT_LIMIT = 100

SORTS: Mapping[str, str] = {
    "deal": "l.deal_score IS NULL, l.deal_score DESC, l.savings_eur_minor DESC",
    "discount": "l.discount_pct IS NULL, l.discount_pct DESC",
    "savings": "l.savings_eur_minor IS NULL, l.savings_eur_minor DESC",
    "price": "l.price_eur_minor IS NULL, l.price_eur_minor ASC",
    "total": "l.total_cost_eur_minor IS NULL, l.total_cost_eur_minor ASC",
    "newest": "l.first_seen_at DESC",
    "ending": "l.item_end_at IS NULL, l.item_end_at ASC",
}

ALERT_STATES = ("pending", "sending", "delivered", "failed", "shadow")
CONDITION_CLASSES = ("new", "used_working", "refurbished", "for_parts", "unknown")
RISK_LEVELS = ("low", "medium", "high")
BUYING_OPTIONS = ("fixed", "auction")
LISTING_STATUSES = ("active", "not_seen", "unknown")


class BadRequest(ValueError):
    """A client sent parameters the API cannot interpret."""


# --------------------------------------------------------------------- store


@dataclass(frozen=True)
class DashboardOptions:
    database_path: Path
    shadow_mode: bool
    heartbeat_path: Path | None = None
    daily_budget: int | None = None


class DashboardStore:
    """Read queries and decision writes against the scanner database."""

    def __init__(self, options: DashboardOptions) -> None:
        self.options = options

    def _read_connection(self) -> sqlite3.Connection:
        # Read-only URI mode fails fast instead of silently recreating the
        # database file if the scanner container has not started yet.
        uri = f"{self.options.database_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with closing(self._read_connection()) as connection:
            return list(connection.execute(sql, params))

    # ------------------------------------------------------------------ API

    def listings(self, filters: Mapping[str, Any]) -> dict[str, Any]:
        where: list[str] = ["1=1"]
        params: list[Any] = []

        text = str(filters.get("q", "") or "").strip()
        if text:
            where.append("(l.title LIKE ? OR l.product_id LIKE ? OR p.name LIKE ?)")
            like = f"%{text}%"
            params.extend([like, like, like])

        for column, key, choices in (
            ("l.product_id", "product", None),
            ("l.marketplace", "marketplace", None),
            ("l.condition_class", "condition", CONDITION_CLASSES),
            ("l.buying_option", "buying_option", BUYING_OPTIONS),
            ("l.status", "status", LISTING_STATUSES),
            ("l.risk_level", "risk", RISK_LEVELS),
        ):
            values = _multi(filters.get(key), choices, key)
            if values:
                placeholders = ",".join("?" for _ in values)
                where.append(f"{column} IN ({placeholders})")
                params.extend(values)

        category = filters.get("category")
        if category:
            # Categories come from the products catalogue, not the listing row.
            values = _multi(category, None, "category")
            placeholders = ",".join("?" for _ in values)
            where.append(
                f"l.product_id IN (SELECT id FROM products WHERE category IN ({placeholders}))"
            )
            params.extend(values)

        for column, key in (
            ("l.discount_pct", "min_discount"),
            ("l.confidence_score", "min_confidence"),
            ("l.deal_score", "min_score"),
        ):
            value = _number(filters.get(key), key)
            if value is not None:
                where.append(f"{column} IS NOT NULL AND {column} >= ?")
                params.append(value)

        savings = _number(filters.get("min_savings_eur"), "min_savings_eur")
        if savings is not None:
            where.append("l.savings_eur_minor IS NOT NULL AND l.savings_eur_minor >= ?")
            params.append(int(round(savings * 100)))

        max_total = _number(filters.get("max_total_eur"), "max_total_eur")
        if max_total is not None:
            where.append(
                "l.total_cost_eur_minor IS NOT NULL AND l.total_cost_eur_minor <= ?"
            )
            params.append(int(round(max_total * 100)))

        if filters.get("deals_only") == "1":
            where.append(
                "l.value_eur_minor IS NOT NULL AND l.discount_pct IS NOT NULL "
                "AND l.savings_eur_minor IS NOT NULL"
            )
        if filters.get("has_alert") == "1":
            where.append(
                "EXISTS (SELECT 1 FROM alerts a WHERE a.listing_id = l.item_id)"
            )

        ship = str(filters.get("ship", "any") or "any")
        if ship == "ok":
            where.append("l.ship_to_ok = 1 AND l.shipping_known = 1")
        elif ship == "unknown":
            where.append("l.shipping_known = 0")

        ending_hours = _number(filters.get("ending_hours"), "ending_hours")
        if ending_hours is not None:
            cutoff = utc_iso(datetime.now(timezone.utc) + timedelta(hours=ending_hours))
            where.append("l.item_end_at IS NOT NULL AND l.buying_option = 'auction' AND l.item_end_at <= ?")
            params.append(cutoff)

        sort = SORTS.get(str(filters.get("sort", "deal") or "deal"), SORTS["deal"])
        limit = _int(filters.get("limit"), "limit", DEFAULT_LIMIT, MAX_LIMIT)
        offset = _int(filters.get("offset"), "offset", 0, 1_000_000)

        sql = (
            "SELECT l.*, p.name AS product_name, p.category AS product_category "
            "FROM listings l LEFT JOIN products p ON p.id = l.product_id "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY {sort} LIMIT ? OFFSET ?"
        )
        rows = self._query(sql, [*params, limit, offset])
        total_row = self._query(
            "SELECT COUNT(*) AS total FROM listings l "
            "LEFT JOIN products p ON p.id = l.product_id "
            f"WHERE {' AND '.join(where)}",
            params,
        )
        return {
            "total": int(total_row[0]["total"]) if total_row else 0,
            "limit": limit,
            "offset": offset,
            "items": [_listing_dict(row) for row in rows],
        }

    def alerts(self, filters: Mapping[str, Any]) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        values = _multi(filters.get("state"), ALERT_STATES, "state")
        if values:
            placeholders = ",".join("?" for _ in values)
            where.append(f"a.state IN ({placeholders})")
            params.extend(values)
        predicate = f"WHERE {' AND '.join(where)} " if where else ""
        limit = _int(filters.get("limit"), "limit", DEFAULT_LIMIT, MAX_LIMIT)

        sql = (
            "SELECT a.id, a.kind, a.alert_key, a.state, a.priority, a.payload_json, "
            "a.attempts, a.next_attempt_at, a.last_error, a.created_at, a.delivered_at, "
            "l.item_id, l.title, l.url, l.marketplace, l.total_cost_eur_minor, "
            "l.savings_eur_minor, l.discount_pct, l.status "
            "FROM alerts a LEFT JOIN listings l ON l.item_id = a.listing_id "
            f"{predicate}ORDER BY a.created_at DESC LIMIT ?"
        )
        rows = self._query(sql, [*params, limit])
        return {"items": [_alert_dict(row) for row in rows]}

    def stats(self) -> dict[str, Any]:
        def one(sql: str, params: Sequence[Any] = ()) -> int:
            row = self._query(sql, params)
            return int(row[0]["n"]) if row else 0

        by_status = {
            row["status"]: int(row["n"])
            for row in self._query(
                "SELECT status, COUNT(*) AS n FROM listings GROUP BY status"
            )
        }
        by_state = {
            row["state"]: int(row["n"])
            for row in self._query("SELECT state, COUNT(*) AS n FROM alerts GROUP BY state")
        }
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        calls_row = self._query(
            "SELECT COALESCE(SUM(calls), 0) AS n FROM api_calls WHERE day=?",
            (today,),
        )
        last_run = self._query(
            """
            SELECT started_at, outcome, query, marketplace, items_returned, error
            FROM search_runs ORDER BY started_at DESC LIMIT 1
            """
        )
        used_today = int(calls_row[0]["n"]) if calls_row else 0
        stats: dict[str, Any] = {
            "generated_at": utc_iso(datetime.now(timezone.utc)),
            "shadow_mode": self.options.shadow_mode,
            "listings": {
                "total": sum(by_status.values()),
                "active": by_status.get("active", 0),
                "not_seen": by_status.get("not_seen", 0),
                "unknown": by_status.get("unknown", 0),
                "new_today": one(
                    "SELECT COUNT(*) AS n FROM listings WHERE substr(first_seen_at,1,10)=?",
                    (today,),
                ),
                "deals": one(
                    "SELECT COUNT(*) AS n FROM listings WHERE deal_score IS NOT NULL "
                    "AND status='active'"
                ),
            },
            "alerts": {
                "pending": by_state.get("pending", 0),
                "sending": by_state.get("sending", 0),
                "delivered": by_state.get("delivered", 0),
                "failed": by_state.get("failed", 0),
                "shadow": by_state.get("shadow", 0),
            },
            "api": {
                "used_today": used_today,
                "budget": self.options.daily_budget,
            },
            "last_run": _dict_row(last_run[0]) if last_run else None,
            "heartbeat": _heartbeat(self.options.heartbeat_path),
        }
        return stats

    def products(self) -> dict[str, Any]:
        rows = self._query(
            "SELECT id, name, category, priority FROM products ORDER BY name"
        )
        items = [_dict_row(row) for row in rows]
        categories: dict[str, int] = {}
        for item in items:
            categories[item["category"]] = categories.get(item["category"], 0) + 1
        return {"items": items, "categories": sorted(categories)}

    def recent_runs(self, limit: int = 30) -> dict[str, Any]:
        """Recent scheduled searches, so scans run anywhere stay visible."""
        rows = self._query(
            "SELECT id, job_key, strategy, query, marketplace, product_id, "
            "started_at, finished_at, outcome, http_status, items_returned, "
            "new_items, pages, error FROM search_runs "
            "ORDER BY started_at DESC LIMIT ?",
            (int(limit),),
        )
        return {"items": [_dict_row(row) for row in rows]}

    def alerts_since(self, since_iso: str, limit: int = 5) -> list[dict[str, Any]]:
        """Alerts created at/after a timestamp — the deals a manual scan found.

        Timestamps come from the same fixed-width UTC format everywhere, so a
        lexical SQL comparison is safe. Candidate states include ``shadow``:
        in shadow mode a candidate scan should still show what it found.
        """
        rows = self._query(
            "SELECT a.id, a.kind, a.state, a.created_at, "
            "l.item_id, l.title, l.url, l.total_cost_eur_minor, "
            "l.savings_eur_minor, l.discount_pct "
            "FROM alerts a LEFT JOIN listings l ON l.item_id = a.listing_id "
            "WHERE a.created_at >= ? "
            "AND a.state IN ('pending', 'delivered', 'shadow') "
            "ORDER BY a.priority DESC, a.created_at ASC LIMIT ?",
            (str(since_iso), int(limit)),
        )
        return [_dict_row(row) for row in rows]

    def record_decision(
        self, item_id: str, decision: str, reason: str | None = None
    ) -> dict[str, Any]:
        if decision not in DECISIONS:
            raise BadRequest(
                f"unknown decision {decision!r}; expected one of {', '.join(DECISIONS)}"
            )
        item_id = item_id.strip()
        if not item_id:
            raise BadRequest("item_id is required")
        reason = (reason or "").strip() or None
        database = Database(self.options.database_path)
        repository = Repository(database)
        try:
            repository.record_decision(
                listing_id=item_id, decision=decision, reason=reason
            )
        finally:
            database.close()
        return {"ok": True, "item_id": item_id, "decision": decision}


# ------------------------------------------------------------------- helpers


def _multi(value: Any, allowed: Sequence[str] | None, key: str) -> list[str]:
    if value is None:
        return []
    text = str(value).strip()
    if not text or text == "any":
        return []
    items = [entry.strip() for entry in text.split(",") if entry.strip()]
    if allowed is not None:
        for entry in items:
            if entry not in allowed:
                raise BadRequest(f"{key}: unknown value {entry!r}")
    return items


def _number(value: Any, key: str) -> float | None:
    if value in (None, "", "any"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"{key}: expected a number, got {value!r}") from exc


def _int(value: Any, key: str, default: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"{key}: expected an integer, got {value!r}") from exc
    return max(0, min(number, maximum))


def _dict_row(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _listing_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = _dict_row(row)
    try:
        item["risk_flags"] = list(json.loads(item.get("risk_flags_json") or "[]"))
    except (ValueError, TypeError):
        item["risk_flags"] = []
    item.pop("risk_flags_json", None)
    item.pop("aspects_json", None)
    item.pop("evaluation_json", None)
    item["risk_flags"] = [str(flag) for flag in item["risk_flags"]]
    return item


def _alert_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = _dict_row(row)
    try:
        item["payload"] = json.loads(item.get("payload_json") or "{}")
    except (ValueError, TypeError):
        item["payload"] = {}
    item["payload"] = item["payload"] if isinstance(item["payload"], dict) else {}
    item["message"] = str(item["payload"].get("message", ""))
    item.pop("payload_json", None)
    return item


def _heartbeat(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


# -------------------------------------------------------------------- server


class GuiHandler(BaseHTTPRequestHandler):
    """HTTP handler bound to a :class:`DashboardStore` via ``store``."""

    store: DashboardStore
    scan_runner: Any = None
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("http %s %s", format % args, self.address_string())

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_query(self) -> dict[str, str]:
        parsed = urlsplit(self.path)
        return {
            key: values[0]
            for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
        }

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise BadRequest("invalid Content-Length") from exc
        if length <= 0 or length > 100_000:
            raise BadRequest("request body is missing or too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequest("request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise BadRequest("request body must be a JSON object")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        try:
            split = urlsplit(self.path)
            if split.path == "/" or split.path == "/index.html":
                body = PAGE_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif split.path == "/api/stats":
                self._send_json(self.store.stats())
            elif split.path == "/api/products":
                self._send_json(self.store.products())
            elif split.path == "/api/listings":
                self._send_json(self.store.listings(self._read_query()))
            elif split.path == "/api/alerts":
                self._send_json(self.store.alerts(self._read_query()))
            elif split.path == "/api/scan":
                if self.scan_runner is None:
                    self._send_json({"error": "manual scans are not enabled here"}, status=503)
                    return
                scan_status = self.scan_runner.status()
                candidates: list[dict[str, Any]] = []
                if scan_status.get("started_at") and not scan_status.get("running"):
                    candidates = self.store.alerts_since(
                        str(scan_status["started_at"]), limit=5
                    )
                self._send_json(
                    {
                        "scan": scan_status,
                        "candidates": candidates,
                        "recent_runs": self.store.recent_runs(30),
                    }
                )
            else:
                self._send_json({"error": "not found"}, status=404)
        except BadRequest as exc:
            self._send_json({"error": str(exc)}, status=400)
        except sqlite3.OperationalError as exc:
            log.warning("sqlite error on %s: %s", self.path, exc)
            self._send_json(
                {
                    "error": (
                        "scanner database is not reachable; start the scanner "
                        "and check the data volume"
                    )
                },
                status=503,
            )
        except Exception:  # boundary: surface, never crash the server thread
            log.exception("request failed: %s", self.path)
            self._send_json({"error": "internal error"}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlsplit(self.path).path
            if path not in ("/api/decision", "/api/scan", "/api/scan/notify-test"):
                self._send_json({"error": "not found"}, status=404)
                return
            payload = self._read_json_body()
            if path == "/api/decision":
                result = self.store.record_decision(
                    str(payload.get("item_id", "")),
                    str(payload.get("decision", "")),
                    payload.get("reason"),
                )
            elif path == "/api/scan":
                if self.scan_runner is None:
                    self._send_json(
                        {"error": "manual scans are not enabled in this process"},
                        status=503,
                    )
                    return
                try:
                    limit = int(payload.get("limit", 8))
                except (TypeError, ValueError) as exc:
                    raise BadRequest("limit must be an integer") from exc
                try:
                    result = self.scan_runner.trigger(
                        str(payload.get("kind", "new_listings") or "new_listings"),
                        payload.get("product_id"),
                        limit,
                    )
                except ScanBusy as exc:
                    self._send_json({"error": str(exc)}, status=409)
                    return
                except ScanCooldown as exc:
                    self._send_json(
                        {"error": str(exc), "retry_after": exc.seconds_left},
                        status=409,
                    )
                    return
                except ValueError as exc:
                    raise BadRequest(str(exc)) from exc
            else:  # /api/scan/notify-test
                if self.scan_runner is None:
                    self._send_json(
                        {"error": "notification tests are not enabled in this process"},
                        status=503,
                    )
                    return
                result = self.scan_runner.test_notify()
            self._send_json(result)
        except BadRequest as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception:  # boundary: surface, never crash the server thread
            log.exception("request failed: %s", self.path)
            self._send_json({"error": "internal error"}, status=500)


def make_handler(
    store: DashboardStore, scan_runner: Any = None
) -> type[GuiHandler]:
    class BoundHandler(GuiHandler):
        bound_store = store

    BoundHandler.store = store
    BoundHandler.scan_runner = scan_runner
    return BoundHandler


class GuiServer:
    def __init__(
        self,
        options: DashboardOptions,
        *,
        watchdog: Any = None,
        scan_runner: Any = None,
    ) -> None:
        self.options = options
        self.store = DashboardStore(options)
        self.watchdog = watchdog
        self.scan_runner = scan_runner

    def serve_forever(self, host: str, port: int) -> None:
        httpd = ThreadingHTTPServer((host, port), make_handler(self.store, self.scan_runner))
        thread = None
        if self.watchdog is not None:
            self.watchdog.stop.clear()
            thread = threading.Thread(
                target=self.watchdog.run_forever,
                name="heartbeat-watchdog",
                daemon=True,
            )
            thread.start()
        log.info(
            "dashboard listening on http://%s:%s (database: %s)",
            host,
            port,
            self.options.database_path,
        )
        try:
            httpd.serve_forever()
        finally:
            if thread is not None:
                self.watchdog.stop.set()
                thread.join(timeout=5)
            httpd.server_close()
