"""Web dashboard API tests against a seeded temporary database."""

from __future__ import annotations

import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from src.gui.page import PAGE_HTML
from src.gui.server import DashboardOptions, DashboardStore, make_handler


@pytest.fixture()
def seeded_db(tmp_path: Path):
    """Create a schema-initialized database with known rows."""
    from src.storage.database import Database
    from src.storage.repository import Repository

    path = tmp_path / "scanner.db"
    database = Database(path)
    database.initialize()
    Repository(database)

    now = "2026-10-28T12:00:00.000Z"
    rows = [
        dict(
            item_id="IT-DEAL", marketplace="EBAY_IT",
            title="NVIDIA GeForce RTX 3090 usata perfetta",
            normalized_title="nvidia geforce rtx 3090 usata perfetta",
            url="https://www.ebay.it/itm/IT-DEAL",
            price_minor=30_000, currency="EUR", price_eur_minor=30_000,
            shipping_eur_minor=1_500, shipping_known=True, ship_to_ok=True,
            condition_class="used_working", buying_option="fixed",
            bid_count=None, current_bid_eur_minor=None,
            item_end_at=None, risk_level="low", product_id="rtx_3090",
            status="active", total_cost_eur_minor=31_500,
            value_eur_minor=52_000, value_source_kind="verified_sold",
            discount_pct=42.0, savings_eur_minor=20_500,
            deal_score=72.0, confidence_score=0.8,
        ),
        dict(
            item_id="DE-PARTS", marketplace="EBAY_DE",
            title="RTX 3080 defekt für Teile",
            normalized_title="rtx 3080 defekt fuer teile",
            url="https://www.ebay.de/itm/DE-PARTS",
            price_minor=8_000, currency="EUR", price_eur_minor=8_000,
            shipping_eur_minor=None, shipping_known=False, ship_to_ok=None,
            condition_class="for_parts", buying_option="fixed",
            bid_count=None, current_bid_eur_minor=None,
            item_end_at=None, risk_level="high", product_id=None,
            status="active", total_cost_eur_minor=None,
            value_eur_minor=None, value_source_kind=None,
            discount_pct=None, savings_eur_minor=None,
            deal_score=None, confidence_score=None,
        ),
        dict(
            item_id="IT-AUCTION", marketplace="EBAY_IT",
            title="Auction RTX 3090",
            normalized_title="auction rtx 3090",
            url="https://www.ebay.it/itm/IT-AUCTION",
            price_minor=None, currency="EUR", price_eur_minor=None,
            shipping_eur_minor=None, shipping_known=False, ship_to_ok=None,
            condition_class="used_working", buying_option="auction",
            bid_count=3, current_bid_eur_minor=25_000,
            item_end_at="2026-10-29T15:00:00.000Z",
            risk_level="medium", product_id="rtx_3090",
            status="active", total_cost_eur_minor=None,
            value_eur_minor=None, value_source_kind=None,
            discount_pct=None, savings_eur_minor=None,
            deal_score=None, confidence_score=None,
        ),
    ]
    shipping_json = None
    with database.transaction() as connection:
        for row in rows:
            shipping_json = "[]"
            connection.execute(
                """
                INSERT INTO listings(
                    item_id, marketplace, title, normalized_title, url,
                    price_minor, currency, price_eur_minor,
                    shipping_eur_minor, shipping_known, ship_to_ok,
                    condition_class, buying_option, bid_count,
                    current_bid_eur_minor, item_end_at, risk_level,
                    product_id, status, first_seen_at, last_seen_at,
                    total_cost_eur_minor, value_eur_minor, value_source_kind,
                    discount_pct, savings_eur_minor, deal_score,
                    confidence_score, risk_flags_json
                ) VALUES (
                    :item_id, :marketplace, :title, :normalized_title, :url,
                    :price_minor, :currency, :price_eur_minor,
                    :shipping_eur_minor, :shipping_known, :ship_to_ok,
                    :condition_class, :buying_option, :bid_count,
                    :current_bid_eur_minor, :item_end_at, :risk_level,
                    :product_id, :status, :first_seen_at, :last_seen_at,
                    :total_cost_eur_minor, :value_eur_minor, :value_source_kind,
                    :discount_pct, :savings_eur_minor, :deal_score,
                    :confidence_score, '[]'
                )
                ON CONFLICT(item_id) DO NOTHING
                """,
                {**row, "first_seen_at": now, "last_seen_at": now,
                 "shipping_eur_minor": row.get("shipping_eur_minor"),
                 "ship_to_ok": row.get("ship_to_ok"),
                 "deal_score": row.get("deal_score"),
                 "confidence_score": row.get("confidence_score"),
                 "value_source_kind": row.get("value_source_kind")},
            )

    # Alerts for the deal row
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO alerts(listing_id, kind, alert_key, state, priority,
                               payload_json, created_at, updated_at, delivered_at)
            VALUES('IT-DEAL', 'new_deal', 'IT-DEAL:new_deal:v1', 'delivered', 3,
                   '{"message":"RTX 3090 at 300 EUR, est 520"}',
                   :now, :now, :now)
            """,
            {"now": now},
        )
    yield path
    database.close()


@pytest.fixture()
def server(seeded_db):
    options = DashboardOptions(
        database_path=seeded_db,
        shadow_mode=False,
        heartbeat_path=None,
        daily_budget=4000,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(DashboardStore(options)))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def test_listings_returns_deals_filtered_by_min_discount(server):
    response = httpx.get(f"{server}/api/listings?min_discount=20", timeout=5)
    assert response.status_code == 200
    body = response.json()
    ids = [item["item_id"] for item in body["items"]]
    assert ids == ["IT-DEAL"]
    item = body["items"][0]
    assert item["risk_flags"] == []
    assert item["savings_eur_minor"] == 20_500


def test_listing_filters_combine(server):
    parts = httpx.get(f"{server}/api/listings?risk=high&limit=100", timeout=5).json()
    assert [i["item_id"] for i in parts["items"]] == ["DE-PARTS"]

    auction = httpx.get(
        f"{server}/api/listings?buying_option=auction&sort=ending", timeout=5
    ).json()
    assert [i["item_id"] for i in auction["items"]] == ["IT-AUCTION"]

    text = httpx.get(f"{server}/api/listings?q=3090", timeout=5).json()
    assert set(i["item_id"] for i in text["items"]) == {"IT-DEAL", "IT-AUCTION"}

    ok = httpx.get(f"{server}/api/listings?ship=ok", timeout=5).json()
    assert [i["item_id"] for i in ok["items"]] == ["IT-DEAL"]


def test_listing_condition_multi_select(server):
    """Condition accepts a comma-separated subset, as the GUI chip group sends."""
    mixed = httpx.get(
        f"{server}/api/listings?condition=used_working,unknown", timeout=5
    ).json()
    assert {i["item_id"] for i in mixed["items"]} == {"IT-DEAL", "IT-AUCTION"}

    parts = httpx.get(f"{server}/api/listings?condition=for_parts", timeout=5).json()
    assert {i["item_id"] for i in parts["items"]} == {"DE-PARTS"}

    bad = httpx.get(f"{server}/api/listings?condition=used_working,newlike", timeout=5)
    assert bad.status_code == 400
    assert "condition" in bad.json()["error"]


def test_listing_sort_and_invalid_params(server):
    cheapest = httpx.get(
        f"{server}/api/listings?sort=price", timeout=5
    ).json()
    # ascending price: DE-PARTS (80 EUR) before IT-DEAL (300 EUR)
    assert [i["item_id"] for i in cheapest["items"]][:2] == ["DE-PARTS", "IT-DEAL"]

    response = httpx.get(f"{server}/api/listings?sort=nope", timeout=5)
    assert response.status_code == 200  # falls back to default sort

    bad = httpx.get(f"{server}/api/listings?min_discount=panda", timeout=5)
    assert bad.status_code == 400
    assert "min_discount" in bad.json()["error"]


def test_alerts_endpoint_includes_message(server):
    body = httpx.get(f"{server}/api/alerts", timeout=5).json()
    assert len(body["items"]) == 1
    alert = body["items"][0]
    assert alert["state"] == "delivered"
    assert "RTX 3090" in alert["message"]
    assert alert["title"].startswith("NVIDIA")


def test_stats_shape(server):
    stats = httpx.get(f"{server}/api/stats", timeout=5).json()
    assert stats["listings"]["active"] == 3
    assert stats["alerts"]["delivered"] == 1
    assert stats["api"] == {"used_today": 0, "budget": 4000}
    assert stats["shadow_mode"] is False


def test_decision_endpoint_records_feedback(server, tmp_path):
    response = httpx.post(
        f"{server}/api/decision",
        json={"item_id": "IT-DEAL", "decision": "good_deal"},
        timeout=5,
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True

    bad = httpx.post(
        f"{server}/api/decision",
        json={"item_id": "IT-DEAL", "decision": "burn_it"},
        timeout=5,
    )
    assert bad.status_code == 400

    empty = httpx.post(
        f"{server}/api/decision", json={"item_id": "", "decision": "good_deal"},
        timeout=5,
    )
    assert empty.status_code == 400


def test_marketplace_dropdown_lists_all_supported_markets():
    """The dashboard dropdown is generated, so non-default sites appear too."""
    assert 'value="EBAY_IT"' in PAGE_HTML
    assert 'value="EBAY_GB"' in PAGE_HTML
    assert 'value="EBAY_US"' in PAGE_HTML
    assert "<!--MARKET_OPTIONS-->" not in PAGE_HTML


def test_marketplace_dropdown_lists_all_supported_markets():
    """The dashboard dropdown is generated, so non-default sites appear too."""
    assert 'value="EBAY_IT"' in PAGE_HTML
    assert 'value="EBAY_GB"' in PAGE_HTML
    assert 'value="EBAY_US"' in PAGE_HTML
    assert "<!--MARKET_OPTIONS-->" not in PAGE_HTML


def test_dashboard_html_is_served(server):
    response = httpx.get(server + "/", timeout=5)
    assert response.status_code == 200
    assert "eBay Deal Scanner" in response.text
    assert "text/html" in response.headers["content-type"]


def test_unknown_route_404(server):
    assert httpx.get(f"{server}/api/nope", timeout=5).status_code == 404


def test_gui_server_starts_and_stops_watchdog(seeded_db, monkeypatch):
    """The GUI boots with a watchdog wired in, and stops it cleanly."""
    import src.gui.server as server_module
    from src.gui.server import GuiServer

    class StubWatchdog:
        def __init__(self) -> None:
            self.stop = threading.Event()
            self.started = threading.Event()

        def run_forever(self) -> None:
            self.started.set()
            self.stop.wait(timeout=10)

    module_stop = threading.Event()

    class FakeServer:
        def __init__(self, addr: tuple[str, int], handler: object) -> None:
            self.addr = addr

        def serve_forever(self) -> None:
            module_stop.wait(timeout=10)  # blocks like the real one

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(server_module, "ThreadingHTTPServer", FakeServer)

    dog = StubWatchdog()
    gui = GuiServer(
        DashboardOptions(database_path=seeded_db, shadow_mode=False,
                         heartbeat_path=None, daily_budget=None),
        watchdog=dog,
    )
    httpd_thread = threading.Thread(
        target=gui.serve_forever, args=("127.0.0.1", 0), daemon=True
    )
    httpd_thread.start()
    try:
        # the watchdog thread must already be running before we release the
        # fake server; GuiServer.serve_forever's finally-block then stops it
        assert dog.started.wait(timeout=5), "watchdog thread did not start"
        module_stop.set()
        httpd_thread.join(timeout=15)
        assert not httpd_thread.is_alive(), "serve_forever did not return"
        assert dog.stop.is_set(), "watchdog was not stopped with the server"
    finally:
        dog.stop.set()
        module_stop.set()


def test_store_decision_validation(seeded_db):
    store = DashboardStore(
        DashboardOptions(database_path=seeded_db, shadow_mode=False)
    )
    with pytest.raises(ValueError):
        store.record_decision(item_id="IT-DEAL", decision="not_a_decision")
