"""Tests for the daily digest builder and the scanner-heartbeat watchdog."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.gui.watchdog import Watchdog
from src.monitoring.digest import build_digest, digest_due

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- digest gate
def test_digest_not_due_before_target_hour():
    early = datetime(2026, 10, 29, 7, 0, tzinfo=timezone.utc)  # 08:00 in Rome
    assert digest_due(early, None, 9, "Europe/Rome") is None


def test_digest_due_once_target_hour_passed():
    daytime = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)  # 11:00 in Rome
    assert digest_due(daytime, None, 9, "Europe/Rome") == date(2026, 10, 29)
    # once sent for the day, not again
    assert digest_due(daytime, date(2026, 10, 29), 9, "Europe/Rome") is None


def test_digest_catches_up_after_downtime():
    late = datetime(2026, 10, 30, 22, 0, tzinfo=timezone.utc)
    assert digest_due(late, date(2026, 10, 28), 9, "Europe/Rome") == date(2026, 10, 30)


# ----------------------------------------------------------------- formatting
class _StubSettings:
    shadow_mode: bool

    def __init__(self, shadow: bool) -> None:
        self.shadow_mode = shadow
        self.timezone = "Europe/Rome"


class _StubConfig:
    def __init__(self, shadow: bool = True) -> None:
        self.settings = _StubSettings(shadow)


def _seeded_repository(tmp_path: Path):
    from src.storage.database import Database
    from src.storage.repository import Repository

    database = Database(tmp_path / "scanner.db")
    database.initialize()
    Repository(database)

    now = "2026-10-29T11:30:00.000Z"
    rows = [
        dict(
            item_id="IT-DEAL", marketplace="EBAY_IT",
            title="NVIDIA GeForce RTX 3090 usata",
            normalized_title="nvidia geforce rtx 3090 usata",
            url="https://www.ebay.it/itm/IT-DEAL",
            price_minor=30_000, currency="EUR", price_eur_minor=30_000,
            shipping_eur_minor=1_500, shipping_known=True, ship_to_ok=True,
            condition_class="used_working", buying_option="fixed",
            risk_level="low", product_id="rtx_3090",
            status="active", total_cost_eur_minor=31_500,
            discount_pct=39.0, savings_eur_minor=20_500, deal_score=88.0,
            confidence_score=0.8, first_seen_at=now, last_seen_at=now,
        ),
        dict(
            item_id="IT-CHEAP", marketplace="EBAY_IT",
            title="Ryzen 5800X3D nuova", normalized_title="ryzen 5800x3d nuova",
            url="https://www.ebay.it/itm/IT-CHEAP",
            price_minor=20_000, currency="EUR", price_eur_minor=20_000,
            shipping_eur_minor=1_000, shipping_known=True, ship_to_ok=True,
            condition_class="new", buying_option="fixed",
            risk_level="low", product_id="ryzen_5800x3d",
            status="active", total_cost_eur_minor=21_000,
            discount_pct=15.0, savings_eur_minor=4_000, deal_score=51.0,
            confidence_score=0.6, first_seen_at=now, last_seen_at=now,
        ),
    ]
    with database.transaction() as connection:
        for row in rows:
            connection.execute(
                """
                INSERT INTO listings(
                    item_id, marketplace, title, normalized_title, url,
                    price_minor, currency, price_eur_minor,
                    shipping_eur_minor, shipping_known, ship_to_ok,
                    condition_class, buying_option, risk_level,
                    product_id, status, total_cost_eur_minor,
                    discount_pct, savings_eur_minor, deal_score,
                    confidence_score, first_seen_at, last_seen_at
                ) VALUES (
                    :item_id, :marketplace, :title, :normalized_title, :url,
                    :price_minor, :currency, :price_eur_minor,
                    :shipping_eur_minor, :shipping_known, :ship_to_ok,
                    :condition_class, :buying_option, :risk_level,
                    :product_id, :status, :total_cost_eur_minor,
                    :discount_pct, :savings_eur_minor, :deal_score,
                    :confidence_score, :first_seen_at, :last_seen_at
                )
                """,
                row,
            )
        connection.execute(
            """
            INSERT INTO alerts(listing_id, kind, alert_key, state, priority,
                               payload_json, created_at, updated_at, delivered_at)
            VALUES('IT-DEAL', 'new_deal', 'IT-DEAL:new_deal:v1', 'delivered', 3,
                   '{"message":"deal"}', :now, :now, :now)
            """,
            {"now": now},
        )
        connection.execute(
            """
            INSERT INTO user_decisions(listing_id, decision, created_at)
            VALUES('IT-DEAL', 'good_deal', '2026-10-29T10:00:00.000Z')
            """
        )
        connection.execute(
            """
            INSERT INTO user_decisions(listing_id, decision, created_at)
            VALUES('IT-DEAL', 'too_risky', '2026-10-28T10:00:00.000Z')
            """
        )
    repository = Repository(database)
    yield repository
    database.close()


def test_build_digest_lists_top_candidates_and_shadow_label(tmp_path):
    for repo in _seeded_repository(tmp_path):
        text = build_digest(repo, _StubConfig(shadow=True), NOW)
        assert "shadow mode" in text
        assert "NVIDIA GeForce RTX 3090" in text
        assert "https://www.ebay.it/itm/IT-DEAL" in text
        assert "delivered 1" in text
        assert "Review (7d): 1 positive / 1 rejected" in text


def test_build_digest_respects_top_limit(tmp_path):
    for repo in _seeded_repository(tmp_path):
        text = build_digest(repo, _StubConfig(shadow=False), NOW, top=1)
        assert "NVIDIA GeForce RTX 3090" in text
        assert "Ryzen 5800X3D" not in text


# ------------------------------------------------------------------- watchdog
def _heartbeat(path: Path, when: datetime) -> Path:
    path.write_text(
        json.dumps({"updated_at": when.isoformat().replace("+00:00", "Z")}),
        encoding="utf-8",
    )
    return path


def test_watchdog_stale_nag_and_recovered(tmp_path: Path):
    heartbeat = tmp_path / "heartbeat.json"
    _heartbeat(heartbeat, NOW - timedelta(hours=1, minutes=10))  # stale already

    sent: list[str] = []
    dog = Watchdog(
        heartbeat,
        max_age_seconds=900,
        interval_seconds=60,
        nag_minutes=240,
        notify=sent.append,
    )

    # a stale heartbeat file found at startup is reported immediately
    assert dog.check_once(NOW) == "stale"
    # no repeat until the nag interval passes
    assert dog.check_once(NOW + timedelta(minutes=6)) is None
    heartbeat = _heartbeat(heartbeat, NOW - timedelta(seconds=30))
    assert dog.check_once(NOW + timedelta(minutes=7)) == "recovered"

    heartbeat = _heartbeat(heartbeat, NOW - timedelta(hours=1))
    assert dog.check_once(NOW + timedelta(minutes=8)) == "stale"
    assert dog.check_once(NOW + timedelta(minutes=120)) is None
    # nag repeats once the interval since the stale report has elapsed
    assert dog.check_once(NOW + timedelta(hours=4, minutes=10)) == "nag"

    assert len(sent) == 4
    assert "heartbeat" in sent[0]
    assert "again" in sent[1]  # recovered
    assert "Still no" in sent[3]


def test_watchdog_missing_file_is_stale_then_nags(tmp_path: Path):
    sent: list[str] = []
    heart_path = tmp_path / "heartbeat.json"
    dog = Watchdog(
        heart_path,
        max_age_seconds=900,
        nag_minutes=240,
        notify=sent.append,
    )
    assert dog.age_seconds(NOW) is None
    # missing file stays silent: the scanner may just not have started yet
    assert dog.check_once(NOW) is None
    assert dog.check_once(NOW + timedelta(hours=24)) == "nag"
    assert "heartbeat" in sent[0]

    # scanner starts writing a fresh heartbeat -> recovery notice
    _heartbeat(heart_path, NOW + timedelta(hours=24) - timedelta(seconds=15))
    assert dog.check_once(NOW + timedelta(hours=24, minutes=1)) == "recovered"
