import time
from datetime import datetime, timezone

import httpx
import pytest

from src.config import load_config
from src.ebay.auth import EbayAuth, EbayAuthError
from src.ebay.quota import QuotaExceeded, QuotaLedger

from tests.test_matcher import ENV, PROJECT_ROOT


class FakeRepo:
    def __init__(self):
        self.calls = {}

    def calls_today(self, day, endpoint=None):
        if endpoint is None:
            return sum(self.calls.get((day, ep, mp), 0) for (d, ep, mp) in list(self.calls) if d == day)
        return self.calls.get((day, endpoint, ""), 0)

    def record_api_call(self, day, endpoint, marketplace=""):
        key = (day, endpoint, marketplace)
        self.calls[key] = self.calls.get(key, 0) + 1


@pytest.fixture(scope="module")
def settings():
    return load_config(ENV, base_dir=PROJECT_ROOT).settings


def test_token_is_requested_once_and_cached(settings):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "token-1", "expires_in": 7200},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    auth = EbayAuth(settings, http)
    first = auth.get_token()
    second = auth.get_token()
    assert first == second == "token-1"
    assert len(requests) == 1
    http.close()


def test_bad_credentials_raise_ebay_auth_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    auth = EbayAuth(settings, http)
    with pytest.raises(EbayAuthError):
        auth.get_token()
    http.close()


def test_missing_credentials_raise_without_http(settings):
    empty = type(settings)(
        ebay_client_id="",
        ebay_client_secret="",
        ebay_environment="production",
        telegram_bot_token="",
        telegram_chat_id="",
        ship_to_country="IT",
        delivery_postal_code=None,
        database_path=settings.database_path,
        config_dir=settings.config_dir,
        log_level="INFO",
        timezone="UTC",
        shadow_mode=True,
        daily_call_budget=None,
        health_max_age_seconds=900,
        http_timeout_seconds=20.0,
        http_max_attempts=3,
    )
    http = httpx.Client()
    auth = EbayAuth(empty, http)
    with pytest.raises(EbayAuthError):
        auth.get_token()
    http.close()


def test_quota_ledger_refuses_calls_beyond_budget():
    repo = FakeRepo()
    ledger = QuotaLedger(repo, daily_budget=5, reserve_calls=0)
    now = datetime.now(timezone.utc)
    for _ in range(5):
        ledger.consume("browse_search", "EBAY_IT")
    with pytest.raises(QuotaExceeded):
        ledger.consume("browse_search", "EBAY_IT")
    assert repo.calls_today(now.strftime("%Y-%m-%d")) == 5


def test_quota_rollover_resets_usage():
    repo = FakeRepo()
    fixed_now = datetime(2026, 10, 28, 23, 59, 0, tzinfo=timezone.utc)

    class Clock:
        def __init__(self):
            self.current = fixed_now

    clock = Clock()
    ledger = QuotaLedger(
        repo,
        daily_budget=2,
        reserve_calls=0,
        clock=lambda: clock.current,
    )
    ledger.consume("browse_search", "EBAY_IT")
    ledger.consume("browse_search", "EBAY_IT")
    assert not ledger.can_spend()
    from datetime import timedelta

    clock.current = fixed_now + timedelta(days=1)
    assert ledger.can_spend()


def test_reserve_keeps_search_budget_below_total():
    repo = FakeRepo()
    ledger = QuotaLedger(repo, daily_budget=10, reserve_calls=3)
    status = ledger.status()
    assert status.search_remaining == 7
    assert status.remaining == 10
