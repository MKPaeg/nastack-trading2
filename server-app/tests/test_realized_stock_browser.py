"""Read-only stock aggregation against a disposable database, never live orders."""

import socket
import threading
import time
from datetime import date
from decimal import Decimal

import uvicorn
from playwright.sync_api import expect, sync_playwright
from test_trade_list_archive import seed

from nastack_trading.app import create_app
from nastack_trading.models import RealizedSale, TradeOrder, TradePlan, User, UserSettings
from nastack_trading.schemas import Preferences
from nastack_trading.security import create_session
from nastack_trading.trade_gateway import TradeGateway


def test_stock_totals_all_records_restore_selection_and_owner_switch(setup, monkeypatch):
    uid, tid, pid, oid, sid = seed(setup)

    def forbidden(*args, **kwargs):
        raise AssertionError("No broker calls in realized report")

    monkeypatch.setattr(TradeGateway, "request", forbidden)
    monkeypatch.setattr(TradeGateway, "quote", lambda *args: 100)
    monkeypatch.setattr(TradeGateway, "position", lambda *args: {"quantity": 0, "environment": "live"})
    with setup[1].begin() as db:
        user = db.get(User, uid)
        user.role = "trader"
        token = create_session(db, user, 24)
        plan = db.get(TradePlan, pid)
        plan.value = {k: v for k, v in plan.value.items() if k != "account_ciphertext"}
        plan.value = {**plan.value, "archived": True, "active": False}
        for index in range(201):
            db.add(
                TradeOrder(
                    id=f"synthetic-{index}",
                    owner_id=uid,
                    tenant_id=tid,
                    plan_id=pid,
                    request_key=f"synthetic-{index}",
                    status="filled",
                    value={},
                )
            )
        db.flush()
        for index in range(201):
            db.add(
                RealizedSale(
                    owner_id=uid,
                    tenant_id=tid,
                    plan_id=pid,
                    order_id=f"synthetic-{index}",
                    code="123456",
                    broker="namuh",
                    trade_day=date(2026, 10, 26),
                    quantity=1,
                    proceeds=Decimal("101.1"),
                    cost=Decimal("100"),
                    profit=Decimal("1.1"),
                    value={
                        "name": "보존 종목",
                        "reason": "take_profit",
                        "purchase_price": 100,
                        "fill_price": 101.1,
                    },
                )
            )
        viewer = User(
            tenant_id=tid, email="stock-viewer@example.com", role="viewer", active=True, code_hash="unused"
        )
        db.add(viewer)
        db.flush()
        db.add(UserSettings(user_id=viewer.id, tenant_id=tid, value=Preferences().model_dump(mode="json")))
        viewer_token = create_session(db, viewer, 24)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    config = setup[0].model_copy(
        update={
            "allowed_origins": [origin],
            "live_orders_enabled": False,
            "automatic_orders_enabled": False,
            "live_worker_enabled": False,
        }
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.add_cookies([{"name": "trading_session", "value": token, "url": origin}])
            page = context.new_page()
            errors, posts = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
            page.goto(origin)
            page.get_by_role("button", name="종목 매매", exact=True).click()
            page.get_by_role("tab", name="실현손익", exact=True).click()
            selector = page.get_by_label("종목별 실현손익 선택", exact=True)
            selector.select_option("123456")
            table = page.get_by_role("table", name="선택 종목 실현손익 기록 및 합계", exact=True)
            expect(table.locator("tbody tr")).to_have_count(202)
            expect(table.locator("tfoot")).to_contain_text("201.1")
            expect(table.locator("tfoot")).to_contain_text("202건")
            table.locator("tfoot").scroll_into_view_if_needed()
            page.screenshot(path="/private/tmp/nastack-realized-stock-test.png")
            # Archived stock and records beyond the original 200-row display limit still count.
            page.get_by_label("실현손익 증권사", exact=True).select_option("kiwoom")
            expect(table.locator("tbody tr")).to_have_count(1)
            expect(table.locator("tfoot")).to_contain_text("-20")
            with page.expect_response(
                lambda r: r.url.endswith("/settings/screen-state") and r.request.method == "PUT"
            ):
                selector.select_option("")
                selector.select_option("123456")
            page.reload()
            page.get_by_role("button", name="종목 매매", exact=True).click()
            page.get_by_role("tab", name="실현손익", exact=True).click()
            expect(selector).to_have_value("123456")
            expect(table.locator("tbody tr")).to_have_count(202)
            context.add_cookies([{"name": "trading_session", "value": viewer_token, "url": origin}])
            page.reload()
            page.get_by_role("button", name="종목 매매", exact=True).click()
            page.get_by_role("tab", name="실현손익", exact=True).click()
            expect(selector).to_have_value("")
            expect(selector.locator("option")).to_have_count(1)
            expect(page.get_by_role("button", name="과거 체결 반영")).to_be_disabled()
            assert not posts
            assert not errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
