"""Dashboard manual-scan endpoints with a stubbed scanner runtime.

No network: the runtime factory is replaced by a stub, so these tests cover
trigger validation, job claims, run-history recording, concurrency (409) and
the Telegram test hook.
"""

from __future__ import annotations

import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.discovery.queries import SearchOutcome
from src.gui.scanner_actions import ScanRunner
from src.gui.server import DashboardOptions, DashboardStore, make_handler


class StubRepository:
    def __init__(self) -> None:
        self.busy: set[str] = set()
        self.runs: list[dict] = []
        self.jobs: list[dict] = []

    def acquire_job(self, key, now, lock_seconds):
        self.lock_seconds = lock_seconds
        if key in self.busy:
            return False
        self.jobs.append({"kind": "acquire", "key": key})
        return True

    def start_search_run(self, **kwargs):
        self.runs.append({"kind": "start", **kwargs})
        return len(self.runs)

    def finish_search_run(self, run_id, **kwargs):
        self.runs.append({"kind": "finish", "run_id": run_id, **kwargs})

    def finish_job(self, key, **kwargs):
        self.jobs.append({"kind": "finish", "key": key, **kwargs})


class StubQuota:
    def __init__(self, remaining: int = 123) -> None:
        self._remaining = remaining

    def status(self):
        return SimpleNamespace(remaining=self._remaining)


class StubAlerts:
    def __init__(self) -> None:
        self.delivered_calls = 0

    def deliver_pending(self, now):
        self.delivered_calls += 1
        return SimpleNamespace(delivered=1, failed=0, retried_later=0)


class StubNotifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[str] = []
        self._error = error

    def send(self, text):
        if self._error is not None:
            raise self._error
        self.sent.append(text)
        return "ok"

    def close(self) -> None:
        pass


def _profile(strategy: str = "new_listings", product_id: str = "rtx_3090"):
    return SimpleNamespace(
        key=f"{strategy}:EBAY_IT:{product_id}:abc:{strategy}",
        strategy=strategy,
        query="rtx 3090",
        marketplace="EBAY_IT",
        priority="high",
        product_id=product_id,
        bundle_id=None,
        category_id="gpu",
        taxonomy_hint=None,
        use_category_filter=False,
        max_pages=1,
        sort="newlyListed",
        base_interval_seconds=600,
    )


def _stub_runtime(repository: StubRepository, scanner=None, notifier=None):
    return SimpleNamespace(
        config=SimpleNamespace(products=[], marketplaces=["EBAY_IT"]),
        repository=repository,
        scanner=scanner or SimpleNamespace(
            run_search=lambda profile: SearchOutcome(
                status="ok", items_seen=5, new_items=2, pages=1
            )
        ),
        alerts=StubAlerts(),
        notifier=notifier or StubNotifier(),
        quota=StubQuota(),
        close=lambda: None,
    )


@pytest.fixture()
def scan_db(tmp_path: Path):
    from src.storage.database import Database

    path = tmp_path / "scanner.db"
    database = Database(path)
    database.initialize()
    yield database
    database.close()


@pytest.fixture()
def scan_server(scan_db, monkeypatch):
    """Server with a real ScanRunner wired to a stub runtime factory."""
    repository = StubRepository()
    runtime = _stub_runtime(repository)
    runner = ScanRunner(
        SimpleNamespace(), runtime_factory=lambda config: runtime, cooldown_seconds=0
    )
    # The real profile builder needs a fully loaded AppConfig; stub it so the
    # scan selection logic itself stays under test.
    monkeypatch.setattr(
        ScanRunner,
        "select_profiles",
        lambda self, config, kind, product_id, limit: [
            _profile(strategy="product" if kind == "product" else kind,
                     product_id=product_id or "rtx_3090")
        ][:limit],
    )
    options = DashboardOptions(
        database_path=Path(scan_db.path),
        shadow_mode=False,
        heartbeat_path=None,
        daily_budget=4000,
    )
    store = DashboardStore(options)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(store, scan_runner=runner)
    )
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)

    holder = SimpleNamespace(
        base=f"http://127.0.0.1:{port}",
        runner=runner,
        repository=repository,
        runtime=runtime,
    )
    yield holder
    httpd.shutdown()
    httpd.server_close()


def _wait_finished(holder, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not holder.runner.status()["running"]:
            return
        time.sleep(0.05)
    raise AssertionError("scan did not finish in time")


def test_scan_status_before_any_scan(scan_server):
    response = httpx.get(f"{scan_server.base}/api/scan", timeout=5)
    assert response.status_code == 200
    body = response.json()
    assert body["scan"]["running"] is False
    assert body["scan"]["profile_results"] == []
    assert "items" in body["recent_runs"]


def test_scan_trigger_runs_profile_and_records_history(scan_server, scan_db):
    response = httpx.post(
        f"{scan_server.base}/api/scan",
        json={"kind": "new_listings"},
        timeout=5,
    )
    assert response.status_code == 200
    _wait_finished(scan_server)

    status = scan_server.runner.status()
    assert not status["running"]
    assert status["profile_results"][0]["outcome"] == "ok"
    assert status["profile_results"][0]["items"] == 5
    assert status["profile_results"][0]["new"] == 2
    assert status["summary"]["succeeded"] == 1
    assert status["summary"]["alerts"]["delivered"] == 1
    assert status["summary"]["quota_remaining"] == 123

    starts = [r for r in scan_server.repository.runs if r["kind"] == "start"]
    finishes = [r for r in scan_server.repository.runs if r["kind"] == "finish"]
    assert len(starts) == 1
    assert finishes[0]["outcome"] == "ok"
    finished_jobs = [j for j in scan_server.repository.jobs if j["kind"] == "finish"]
    assert finished_jobs[0]["success"] is True

    # The stub repository does not touch SQLite, so prove the history view
    # with a seeded row in the real search_runs table.
    with scan_db.transaction() as connection:
        connection.execute(
            """INSERT INTO search_runs(job_key, strategy, query, marketplace,
                 started_at, finished_at, outcome, items_returned, new_items, pages)
               VALUES('scheduled:EBAY_IT:x', 'new_listings', 'rtx 3080', 'EBAY_IT',
                 '2026-10-29T10:00:00Z', '2026-10-29T10:00:02Z', 'ok', 7, 3, 1)"""
        )
    history = httpx.get(f"{scan_server.base}/api/scan", timeout=5).json()
    items = history["recent_runs"]["items"]
    assert len(items) == 1
    assert items[0]["outcome"] == "ok"
    assert items[0]["job_key"] == "scheduled:EBAY_IT:x"


def test_scan_is_one_at_a_time_conflict(scan_server):
    release = threading.Event()
    entered = threading.Event()

    def slow_search(profile):
        entered.set()
        release.wait(5)
        return SearchOutcome(status="ok", items_seen=1, new_items=0)

    scan_server.runtime.scanner = SimpleNamespace(run_search=slow_search)

    started = httpx.post(
        f"{scan_server.base}/api/scan", json={"kind": "auctions"}, timeout=5
    )
    assert started.status_code == 200

    assert entered.wait(5), "scan thread never started a search"
    conflict = httpx.post(
        f"{scan_server.base}/api/scan", json={"kind": "new_listings"}, timeout=5
    )
    assert conflict.status_code == 409
    assert "already running" in conflict.json()["error"]

    release.set()
    _wait_finished(scan_server)
    second = httpx.post(
        f"{scan_server.base}/api/scan", json={"kind": "new_listings"}, timeout=5
    )
    assert second.status_code == 200
    _wait_finished(scan_server)


def test_scan_busy_job_is_skipped_not_duplicated(scan_server):
    scan_server.repository.busy.add("new_listings:EBAY_IT:rtx_3090:abc:new_listings")
    httpx.post(f"{scan_server.base}/api/scan", json={"kind": "new_listings"}, timeout=5)
    _wait_finished(scan_server)

    status = scan_server.runner.status()
    assert status["profile_results"][0]["outcome"] == "skipped"
    assert "busy" in status["profile_results"][0]["error"]
    assert status["summary"]["busy"] == 1
    assert status["summary"]["succeeded"] == 0


def test_scan_product_requires_product_id(scan_server):
    response = httpx.post(
        f"{scan_server.base}/api/scan", json={"kind": "product"}, timeout=5
    )
    assert response.status_code == 400
    assert "product_id" in response.json()["error"]


def test_scan_rejects_unknown_kind(scan_server):
    response = httpx.post(
        f"{scan_server.base}/api/scan", json={"kind": "teleport"}, timeout=5
    )
    assert response.status_code == 400


def test_scan_over_budget_stops_early(scan_server):
    scan_server.runtime.quota = StubQuota(remaining=0)
    response = httpx.post(
        f"{scan_server.base}/api/scan", json={"kind": "new_listings"}, timeout=5
    )
    assert response.status_code == 200
    _wait_finished(scan_server)

    status = scan_server.runner.status()
    assert status["profile_results"][0]["outcome"] == "skipped"
    assert "budget" in status["profile_results"][0]["error"]
    assert status["summary"]["stopped"] == "daily API budget exhausted"
    # No search was even attempted against the API stub.
    assert not scan_server.repository.runs


def test_notify_test_reports_telegram_failure(scan_server):
    scan_server.runtime.notifier = StubNotifier(
        error=RuntimeError("TELEGRAM_BOT_TOKEN missing")
    )
    response = httpx.post(
        f"{scan_server.base}/api/scan/notify-test", json={}, timeout=5
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "TELEGRAM_BOT_TOKEN" in body["error"]


def test_notify_test_delivers(scan_server):
    response = httpx.post(
        f"{scan_server.base}/api/scan/notify-test", json={}, timeout=5
    )
    assert response.status_code == 200
    assert response.json()["delivered"] is True
    assert scan_server.runtime.notifier.sent


def test_scan_routes_503_without_scan_runner(scan_db):
    options = DashboardOptions(
        database_path=Path(scan_db.path),
        shadow_mode=False,
        heartbeat_path=None,
        daily_budget=4000,
    )
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(DashboardStore(options))
    )
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    try:
        post = httpx.post(f"{base}/api/scan", json={"kind": "auctions"}, timeout=5)
        assert post.status_code == 503
        notify = httpx.post(f"{base}/api/scan/notify-test", json={}, timeout=5)
        assert notify.status_code == 503
        scan = httpx.get(f"{base}/api/scan", timeout=5)
        assert scan.status_code == 503
    finally:
        httpd.shutdown()
        httpd.server_close()


def _mount(database, runner: ScanRunner) -> tuple[str, ThreadingHTTPServer]:
    options = DashboardOptions(
        database_path=Path(database.path),
        shadow_mode=False,
        heartbeat_path=None,
        daily_budget=4000,
    )
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(DashboardStore(options), scan_runner=runner),
    )
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    return f"http://127.0.0.1:{port}", httpd


def test_scan_cooldown_blocks_immediate_rescan(scan_db):
    """After a finished scan, an immediate rescan gets a 409 cooldown."""
    repository = StubRepository()
    runtime = _stub_runtime(repository)
    runner = ScanRunner(
        SimpleNamespace(),
        runtime_factory=lambda config: runtime,
        cooldown_seconds=600,
    )
    base, httpd = _mount(scan_db, runner)
    holder = SimpleNamespace(
        base=base, runner=runner, repository=repository, runtime=runtime
    )
    try:
        first = httpx.post(f"{base}/api/scan", json={"kind": "new_listings"}, timeout=5)
        assert first.status_code == 200
        _wait_finished(holder)

        second = httpx.post(f"{base}/api/scan", json={"kind": "new_listings"}, timeout=5)
        assert second.status_code == 409
        body = second.json()
        assert "cooldown" in body["error"]
        assert 0 < body["retry_after"] <= 600

        status = httpx.get(f"{base}/api/scan", timeout=5).json()
        assert status["scan"]["cooldown_seconds_left"] > 0
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_scan_cooldown_expires(scan_server):
    """The fixture's cooldown of 0 lets rescans go through immediately."""
    for kind in ("new_listings", "variant"):
        response = httpx.post(
            f"{scan_server.base}/api/scan", json={"kind": kind}, timeout=5
        )
        assert response.status_code == 200
        _wait_finished(scan_server)


def test_scan_candidates_show_alerts_created_by_scan(scan_server, scan_db):
    """Alerts created at/after the scan start come back as candidates."""
    started = scan_server.runner.status()["started_at"]
    assert started is None

    # Run a scan (creates no alerts by itself: the stub scraper does not write)
    response = httpx.post(f"{scan_server.base}/api/scan", json={"kind": "new_listings"}, timeout=5)
    assert response.status_code == 200
    _wait_finished(scan_server)
    started = scan_server.runner.status()["started_at"]
    assert started

    with scan_db.transaction() as connection:
        connection.execute(
            """INSERT INTO listings(item_id, marketplace, title, normalized_title,
                url, first_seen_at, last_seen_at, product_id, discount_pct,
                savings_eur_minor, total_cost_eur_minor)
               VALUES('IT-DEAL', 'EBAY_IT', 'Auction lot 3090', 'auction lot 3090',
                'https://www.ebay.it/itm/IT-DEAL', :t, :t, 'rtx_3090', 30.0,
                12000, 30000)""",
            {"t": started},
        )
        connection.execute(
            """INSERT INTO alerts(listing_id, kind, alert_key, state, priority,
                payload_json, created_at, updated_at)
               VALUES('IT-DEAL', 'new_deal', 'IT-DEAL:new_deal:v1', 'pending', 3,
                '{}', :t, :t)""",
            {"t": started},
        )
        # Failed attempts are not candidates, they never got delivered.
        connection.execute(
            """INSERT INTO alerts(listing_id, kind, alert_key, state, priority,
                payload_json, created_at, updated_at)
               VALUES('IT-DEAL', 'new_deal', 'IT-DEAL:new_deal:v2', 'failed', 3,
                '{}', :t, :t)""",
            {"t": started},
        )

    body = httpx.get(f"{scan_server.base}/api/scan", timeout=5).json()
    candidates = body["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["item_id"] == "IT-DEAL"
    assert candidates[0]["state"] == "pending"
    assert candidates[0]["savings_eur_minor"] == 12000
