from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.config import load_config
from src.discovery.queries import build_profiles, plan_schedule
from src.discovery.scheduler import Scheduler, CycleStats
from src.storage.repository import Repository
from src.storage.database import Database

from tests.test_matcher import ENV, PROJECT_ROOT


@pytest.fixture(scope="module")
def config():
    return load_config(ENV, base_dir=PROJECT_ROOT)


def test_profiles_cover_all_products_and_marketplaces(config):
    profiles = build_profiles(config)
    keys = {profile.key for profile in profiles}
    assert len(keys) == len(profiles)  # keys are unique
    strategies = {profile.strategy for profile in profiles}
    assert {"new_listings", "auctions", "variant", "bundle", "discovery"} <= strategies
    # Every product appears on every configured marketplace.
    product_ids = {profile.product_id for profile in profiles if profile.product_id}
    assert product_ids == {product.id for product in config.products}
    marketplaces = {profile.marketplace for profile in profiles}
    assert marketplaces == set(config.marketplaces)


def test_high_priority_products_scan_more_frequently(config):
    profiles = build_profiles(config)
    high_next = min(
        profile.base_interval_seconds
        for profile in profiles
        if profile.priority == "high" and profile.strategy == "new_listings"
    )
    normal_next = min(
        profile.base_interval_seconds
        for profile in profiles
        if profile.priority == "normal" and profile.strategy == "new_listings"
    )
    assert high_next < normal_next


def test_plan_respects_daily_budget(config):
    profiles = build_profiles(config)
    plan = plan_schedule(profiles, config)
    assert plan.available_calls_per_day > 0
    assert plan.planned_calls_per_day <= plan.available_calls_per_day + 1
    assert all(interval >= 60 for interval in plan.intervals.values())
    assert plan.scale_factor >= 1.0


def _repo(tmp_path):
    database = Database(tmp_path / "sched.db")
    database.initialize()
    return Repository(database)


class FakeExecutor:
    def __init__(self):
        self.runs = []

    def run_search(self, profile):
        self.runs.append(profile.key)
        from src.discovery.queries import SearchOutcome

        return SearchOutcome(status="ok", items_seen=1, new_items=0, pages=1)


class FakeQuota:
    def __init__(self, remaining=1_000_000):
        self._remaining = remaining

    def status(self):
        from src.ebay.quota import QuotaStatus

        return QuotaStatus("2026-10-28", 10, 10 + self._remaining, 0)

    def detail_calls_today(self):
        return 0


NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_scheduler_runs_at_most_max_jobs_and_records_success(tmp_path, config):
    profiles = build_profiles(config)
    plan = plan_schedule(profiles, config)
    repo = _repo(tmp_path)
    executor = FakeExecutor()
    scheduler = Scheduler(
        profiles,
        plan,
        repo,
        executor,
        config,
        FakeQuota(),
        clock=NOW,
    )
    scheduler.prepare()
    stats = scheduler.tick(NOW, max_jobs=3)
    assert isinstance(stats, CycleStats)
    assert stats.started == 3
    assert stats.succeeded == 3
    assert len(executor.runs) == 3

    states = repo.get_job_states()
    run_keys = set(executor.runs)
    assert all(states[key]["consecutive_failures"] == 0 for key in run_keys)


def test_scheduler_skips_jobs_when_quota_is_exhausted(tmp_path, config):
    profiles = build_profiles(config)
    plan = plan_schedule(profiles, config)
    repo = _repo(tmp_path)
    scheduler = Scheduler(
        profiles,
        plan,
        repo,
        FakeExecutor(),
        config,
        FakeQuota(remaining=0),
        clock=NOW,
    )
    scheduler.prepare()
    stats = scheduler.tick(NOW, max_jobs=5)
    assert stats.started == 0
    assert stats.skipped_budget >= 1
