"""Multi-instance safety: alert claims and the runner lease.

Two scanner containers share one SQLite database. The properties tested
here are what make that safe:

- claiming an alert moves it out of 'pending' so two deliverers cannot
  both send a Telegram message for the same deal;
- a 'sending' alert stuck after a crash is reclaimable after a grace
  period, and reclaiming counts an attempt so it cannot loop forever;
- the runner lease lets exactly one scanner own the schedule, renews
  re-entrantly and frees the row after its TTL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.alerts.service import AlertService
from src.alerts.telegram import TelegramRateLimited
from src.storage.database import Database
from src.storage.repository import Repository

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
START = NOW - timedelta(days=1)


class _Alerts:
    max_per_day = 20


class _StubConfig:
    def __init__(self) -> None:
        self.alerts = _Alerts()


def _repo(tmp_path: Path, name: str):
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    yield Repository(database)
    database.close()


def test_claim_is_exclusive(tmp_path):
    for repository in _repo(tmp_path, "claim"):
        repository.database.connection.execute(
            """
            INSERT INTO listings(item_id, marketplace, title, normalized_title,
                                 url, currency, condition_class, buying_option,
                                 risk_level, status, first_seen_at, last_seen_at)
        VALUES('IT-1', 'EBAY_IT', 'RTX 3090', 'rtx 3090', 'https://x/1', 'EUR',
               'used_working', 'fixed', 'low', 'active', :s, :s)
            """,
            {"s": START.isoformat()},
        )
        for i in range(3):
            repository.database.connection.execute(
                """
                INSERT INTO alerts(listing_id, kind, alert_key, state, priority,
                                   payload_json, created_at, updated_at)
                VALUES('IT-1', 'new_deal', :key, 'pending', 3, '{}', :s, :s)
                """,
                {"key": f"alert-{i}", "s": START.isoformat()},
            )

        claimed = repository.claim_pending_alerts(NOW, limit=3)
        assert len(claimed) == 3
        assert {a.state for a in claimed} == {"sending"}
        # second deliverer gets nothing while they are 'sending'
        assert repository.claim_pending_alerts(NOW, limit=3) == []
        # and the pending set is empty
        assert repository.pending_alerts_due(NOW) == []


def test_stuck_sending_is_reclaimed_with_attempt_count(tmp_path):
    for repository in _repo(tmp_path, "stuck"):
        repository.database.connection.execute(
            """
            INSERT INTO listings(item_id, marketplace, title, normalized_title,
                                 url, currency, condition_class, buying_option,
                                 risk_level, status, first_seen_at, last_seen_at)
        VALUES('IT-1', 'EBAY_IT', 'RTX 3090', 'rtx 3090', 'https://x/1', 'EUR',
               'used_working', 'fixed', 'low', 'active', :s, :s)
            """,
            {"s": START.isoformat()},
        )
        repository.database.connection.execute(
            """
            INSERT INTO alerts(listing_id, kind, alert_key, state, priority,
                               payload_json, created_at, updated_at)
            VALUES('IT-1', 'new_deal', 'k1', 'pending', 3, '{}', :s, :s)
            """,
            {"s": START.isoformat()},
        )
        first = repository.claim_pending_alerts(NOW)[0]
        assert first.state == "sending"

        # within the grace period: not reclaimable
        later = NOW + timedelta(minutes=2)
        assert repository.claim_pending_alerts(later) == []

        # after the grace period it is reclaimed, and the attempt is counted
        after = NOW + timedelta(minutes=10)
        reclaimed = repository.claim_pending_alerts(after)
        assert len(reclaimed) == 1
        assert reclaimed[0].attempts == 1


def _service(tmp_path: Path, repo: Repository, notifier):
    return AlertService(repo, _StubConfig(), notifier, shadow_mode=False)


def test_delivery_claim_prevents_double_send(tmp_path):
    """A rate-limited send must leave the alert pending-attributed once."""
    for repository in _repo(tmp_path, "delivery"):
        repository.database.connection.execute(
            """
            INSERT INTO listings(item_id, marketplace, title, normalized_title,
                                 url, currency, condition_class, buying_option,
                                 risk_level, status, first_seen_at, last_seen_at)
        VALUES('IT-1', 'EBAY_IT', 'RTX 3090', 'rtx 3090', 'https://x/1', 'EUR',
               'used_working', 'fixed', 'low', 'active', :s, :s)
            """,
            {"s": START.isoformat()},
        )
        repository.database.connection.execute(
            """
            INSERT INTO alerts(listing_id, kind, alert_key, state, priority,
                               payload_json, created_at, updated_at)
            VALUES('IT-1', 'new_deal', 'k1', 'pending', 3, '{}', :s, :s)
            """,
            {"s": START.isoformat()},
        )

        class RateLimitedNotifier:
            configured = True

            def send(self, text):
                raise TelegramRateLimited(2.0, "rate limited")

        service = _service(tmp_path, repository, RateLimitedNotifier())
        stats = service.deliver_pending(NOW)
        assert stats.retried_later >= 1
        row = repository.database.connection.execute(
            "SELECT state, attempts, next_attempt_at FROM alerts WHERE id=1"
        ).fetchone()
        # went back to pending for retry, not stuck in sending
        assert row["state"] == "pending"
        assert row["next_attempt_at"] is not None

        # a second deliverer that runs later can claim exactly once more
        second = repository.claim_pending_alerts(NOW + timedelta(minutes=30))
        assert [a.id for a in second] == [1]


def test_runner_lease_single_owner(tmp_path):
    for repository in _repo(tmp_path, "lease"):
        first_id = "host-a:1000:aaaa"
        second_id = "host-b:2000:bbbb"

        assert repository.acquire_ownership(first_id, NOW, owner_host="host-a")
        # second instance is refused while the first lease is fresh
        assert repository.acquire_ownership(second_id, NOW, owner_host="host-b") is False

        # re-entrant renewal by the owner
        assert repository.acquire_ownership(first_id, NOW + timedelta(seconds=30))
        assert repository.owner_info()["owner_id"] == first_id

        # after the TTL the stale lease can be taken over
        assert repository.acquire_ownership(second_id, NOW + timedelta(minutes=5))
        assert repository.owner_info()["owner_id"] == second_id

        # releasing only works for the owner
        repository.release_ownership(second_id)
        assert repository.owner_info() is None
