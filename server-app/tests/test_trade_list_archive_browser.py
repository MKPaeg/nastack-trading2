"""Listing lifecycle in a real browser against a disposable DB; never live orders."""

import socket
import threading
import time

import uvicorn
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import func, select
from test_trade_list_archive import seed

from nastack_trading.app import create_app
from nastack_trading.models import (
    Instrument,
    RealizedSale,
    TradeOrder,
    TradePlan,
    User,
    UserSettings,
    WatchlistItem,
)
from nastack_trading.schemas import Preferences
from nastack_trading.security import create_session
from nastack_trading.trade_gateway import TradeGateway


def test_remove_reload_readd_preserves_history_and_viewer_isolation(setup, monkeypatch):
    uid, tid, pid, oid, sid = seed(setup)

    def forbidden(*args, **kwargs):
        raise AssertionError("No broker network or orders in browser verification")

    monkeypatch.setattr(TradeGateway, "request", forbidden)
    monkeypatch.setattr(TradeGateway, "quote", lambda *args: 100)
    monkeypatch.setattr(TradeGateway, "position", lambda *args: {"quantity": 0, "environment": "live"})
    with setup[1].begin() as db:
        user = db.get(User, uid)
        user.role = "trader"
        token = create_session(db, user, 24)
        inst = db.scalar(select(Instrument).where(Instrument.tenant_id == tid, Instrument.code == "123456"))
        db.add(WatchlistItem(owner_id=uid, tenant_id=tid, instrument_id=inst.id))
        plan = db.get(TradePlan, pid)
        plan.value = {k: v for k, v in plan.value.items() if k != "account_ciphertext"}
        # Terminal old orders avoid polling unrelated reconciliation in this UI test.
        pending = db.scalar(select(TradeOrder).where(TradeOrder.request_key == "existing-pending"))
        pending.status = "canceled"
        viewer = User(
            tenant_id=tid, email="archive-viewer@example.com", role="viewer", active=True, code_hash="unused"
        )
        db.add(viewer)
        db.flush()
        db.add(UserSettings(user_id=viewer.id, tenant_id=tid, value=Preferences().model_dump(mode="json")))
        db.add(
            TradePlan(
                owner_id=viewer.id,
                tenant_id=tid,
                broker="kiwoom",
                code="123456",
                value={"name": "조회전용 보관", "active": False, "archived": True, "mode": "manual"},
            )
        )
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
            errors = []
            mutations = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda r: mutations.append(r.url) if r.method == "POST" else None)
            page.on("dialog", lambda dialog: dialog.accept())
            page.goto(origin)
            page.get_by_role("button", name="종목 매매", exact=True).click()
            remove = page.get_by_role("button", name="보존 종목 매매 목록에서 제거", exact=True)
            expect(remove).to_be_enabled()
            remove.click()
            summary = page.locator("summary").filter(has_text="제거된 매매 종목 1개")
            expect(summary).to_be_visible()
            expect(remove).to_have_count(0)
            page.reload()
            page.get_by_role("button", name="종목 매매", exact=True).click()
            expect(summary).to_be_visible()
            summary.click()
            restore = page.get_by_role("button", name="보존 종목 매매 목록에 재추가", exact=True)
            expect(restore).to_be_enabled()
            page.screenshot(path="/private/tmp/nastack-trade-archived-test.png", full_page=True)
            with page.expect_response(lambda r: r.url.endswith(f"/plans/{pid}/restore")) as response:
                restore.click()
            assert response.value.json()["id"] == pid and not response.value.json()["active"]
            expect(remove).to_be_enabled()
            # The top add control restores the same existing plan too (no account/API call).
            remove.click()
            expect(summary).to_be_visible()
            page.get_by_label("주문 증권사", exact=True).select_option("kiwoom")
            page.get_by_role("button", name="매매 목록에 추가", exact=True).click()
            expect(remove).to_be_enabled()
            # A new login session still sees the restored plan and old financial history.
            with setup[1].begin() as db:
                new_token = create_session(db, db.get(User, uid), 24)
            context.add_cookies([{"name": "trading_session", "value": new_token, "url": origin}])
            page.reload()
            page.get_by_role("button", name="종목 매매", exact=True).click()
            expect(remove).to_be_enabled()
            with setup[1]() as db:
                assert (
                    db.scalar(select(func.count()).select_from(TradePlan).where(TradePlan.owner_id == uid))
                    == 1
                )
                assert db.get(TradePlan, pid).value["active"] is False
                assert db.get(RealizedSale, sid).profit == -20
                assert db.get(TradeOrder, oid).status == "filled"
            context.add_cookies([{"name": "trading_session", "value": viewer_token, "url": origin}])
            page.reload()
            page.get_by_role("button", name="종목 매매", exact=True).click()
            expect(summary).to_be_visible()
            summary.click()
            expect(
                page.get_by_role("button", name="조회전용 보관 매매 목록에 재추가", exact=True)
            ).to_be_disabled()
            expect(restore).to_have_count(0)
            expect(remove).to_have_count(0)
            assert not any(
                path in url for url in mutations for path in ["/orders/preview", "/orders/confirm", "/cancel"]
            )
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
