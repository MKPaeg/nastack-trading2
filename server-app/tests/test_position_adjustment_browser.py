"""The redesigned table and individual approvals, with every broker operation mocked."""

import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from test_trade_list_archive import seed

from nastack_trading.app import create_app
from nastack_trading.models import TradeOrder, TradePlan, User, UserSettings
from nastack_trading.schemas import Preferences
from nastack_trading.security import create_session
from nastack_trading.trade_gateway import TradeGateway


@pytest.mark.parametrize("role", ["admin", "trader"])
def test_adjustment_ui_explicit_confirmation_restore_and_viewer_isolation(setup, monkeypatch, role):
    monkeypatch.setattr("nastack_trading.trading.market_session", lambda: {"is_open": True})
    uid, tid, pid, oid, sid = seed(setup)
    position = {"quantity": 5, "purchase_price": 100.0, "environment": "live"}
    wire, completed = [], set()

    def forbidden(*args, **kwargs):
        raise AssertionError("No broker network in browser test")

    def submit(self, db, user, plan, account, side, quantity, **kwargs):
        from nastack_trading.order_boundary import before_transmission

        before_transmission()
        wire.append((side, quantity, kwargs.get("limit_price")))
        return f"synthetic-{len(wire)}"

    def fills(self, db, user, order):
        if order.id not in completed:
            completed.add(order.id)
            quantity = order.value["quantity"]
            if order.value["side"] == "buy":
                position["purchase_price"] = (
                    position["quantity"] * position["purchase_price"] + quantity * 110
                ) / (position["quantity"] + quantity)
                position["quantity"] += quantity
            else:
                position["quantity"] -= quantity
        return {"filled_quantity": order.value["quantity"], "fill_price": 110, "remaining_quantity": 0}

    monkeypatch.setattr(TradeGateway, "request", forbidden)
    monkeypatch.setattr(TradeGateway, "quote", lambda *args: 110)
    monkeypatch.setattr(
        TradeGateway,
        "capacity",
        lambda self, db, user, plan, account, side, *args: (
            100 if side == "buy" else position["quantity"],
            11000,
            "live",
        ),
    )
    monkeypatch.setattr(TradeGateway, "position", lambda *args: dict(position))
    monkeypatch.setattr(TradeGateway, "submit", submit)
    monkeypatch.setattr(TradeGateway, "fills", fills)
    with setup[1].begin() as db:
        user = db.get(User, uid)
        user.role = role
        settings = db.get(UserSettings, uid)
        settings.value = {**settings.value, "trade_safety": {"new_orders_locked": False}}
        token = create_session(db, user, 24)
        plan = db.get(TradePlan, pid)
        plan.value = {
            **{k: v for k, v in plan.value.items() if k != "account_ciphertext"},
            "auto_max_quantity": 2,
            "quantity_limit_explicit": True,
            "buy_price": 110,
        }
        for order in db.scalars(select(TradeOrder).where(TradeOrder.owner_id == uid)):
            order.status = "filled" if order.id == oid else "canceled"
        viewer = User(
            tenant_id=tid, email="adjust-viewer@example.com", active=True, role="viewer", code_hash="unused"
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
                value={
                    "name": "조회전용 종목",
                    "active": False,
                    "fraction": 10,
                    "take_profit": 5,
                    "buy_price": 100,
                    "mode": "automatic",
                    "buy_rule": "price",
                },
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
            "live_orders_enabled": True,
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
            context = browser.new_context(viewport={"width": 1700, "height": 1100})
            context.add_cookies([{"name": "trading_session", "value": token, "url": origin}])
            page = context.new_page()
            page.route("**/api/trading/plans/*/quote", lambda route: route.fulfill(json={"snapshot": {"price": 110, "reference_close": 100, "collected_at": "2026-10-27T10:00:00+09:00", "chart": []}}))
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin)
            page.get_by_role("button", name="종목 매매", exact=True).click()
            expect(page.get_by_role("columnheader", name="공통 매매 수량", exact=True)).to_be_visible()
            row = page.locator(f'[data-trade-row="{pid}"]')
            auto_buy = row.get_by_role("group", name="자동매수", exact=True)
            manual_buy = row.get_by_role("group", name="수동 추가매수", exact=True)
            manual_sell = row.get_by_role("group", name="수동 일부매도", exact=True)
            expect(auto_buy.get_by_label("보존 종목 기준 매수가격", exact=True)).to_be_visible()
            expect(manual_buy.get_by_label("보존 종목 추가매수 지정가격", exact=True)).to_be_visible()
            expect(manual_sell.get_by_label("보존 종목 일부매도 지정가격", exact=True)).to_be_visible()
            expect(row.get_by_role("group", name="자동 익절", exact=True).get_by_label("보존 종목 익절 사용", exact=True)).to_be_visible()
            expect(row.get_by_role("group", name="자동 손절", exact=True).get_by_label("보존 종목 손절 사용", exact=True)).to_be_visible()
            condition = auto_buy.get_by_label("보존 종목 자동매수 가격 조건", exact=True)
            expect(condition).to_have_value("at_or_below")
            with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/settings")) as saved_condition:
                condition.select_option("at_or_above")
            assert saved_condition.value.status == 200
            expect(auto_buy).to_contain_text("실제 체결가격 상한은 없습니다.")
            expect(manual_buy.get_by_label("보존 종목 추가매수 지정가격", exact=True)).to_have_value("110")
            assert wire == []  # Choosing/auto-saving a condition never sends orders.
            assert row.bounding_box()["height"] <= 740
            expect(auto_buy.locator(".trade-box-help")).not_to_have_attribute("open", "")
            auto_buy.locator(".trade-box-help summary").click()
            expect(auto_buy.locator(".trade-box-help")).to_have_attribute("open", "")
            auto_buy.locator(".trade-box-help summary").click()
            row.screenshot(path=f"/private/tmp/nastack-trade-compact-row-{role}.png")
            page.screenshot(path=f"/private/tmp/nastack-trade-price-boxes-{role}.png", full_page=True)
            expect(row.get_by_label("보존 종목 매매 유형", exact=True)).to_have_count(0)
            expect(row.get_by_label("보존 종목 반복 매매", exact=True)).to_have_count(0)
            expect(row.get_by_label("보존 종목 예측 기준", exact=True)).to_have_count(0)
            expect(row.get_by_label("보존 종목 매매 수량", exact=True)).to_have_value("2")
            expect(row.locator("td")).to_have_count(6)
            expect(row.locator("td").nth(5).get_by_role("button")).to_have_count(3)
            forecast = row.locator("td").nth(1).get_by_role("group", name="예상 수익률·가격 / 뉴스 평가", exact=True)
            expect(forecast).to_contain_text("저장 예측 없음")
            expect(row.locator(".trade-quote")).to_contain_text("110 원")
            expect(row.get_by_role("group", name="현재가 · 등락률", exact=True)).to_contain_text("110 원")
            assert forecast.bounding_box()["y"] > row.locator(".trade-quote").bounding_box()["y"]
            def assert_card_colors_match(target):
                assert target.evaluate("row=>[...row.querySelectorAll('.trade-setting-box, .trade-setting-box legend, [data-key-metric]')].every(card=>getComputedStyle(card).backgroundColor===getComputedStyle(card.closest('td')).backgroundColor)")
            assert_card_colors_match(row)
            row.locator(".trade-quote").hover()
            assert_card_colors_match(row)
            # Exercise both alternating row palettes without creating any plan.
            row.evaluate("row=>{const copy=row.cloneNode(true);copy.dataset.tradeRow='palette-test';row.after(copy)}")
            palette_row = page.locator('[data-trade-row="palette-test"]')
            assert_card_colors_match(palette_row)
            palette_row.locator(".trade-quote").hover()
            assert_card_colors_match(palette_row)
            palette_row.evaluate("row=>row.remove()")
            widths = row.locator("td").evaluate_all("cells=>cells.map(cell=>cell.getBoundingClientRect().width)")
            assert max(widths)-min(widths)<2
            with page.expect_response(lambda r: r.url.endswith("/orders/preview")) as response:
                page.get_by_role("button", name="보존 종목 추가매수", exact=True).click()
            assert response.value.status == 200, response.value.text()
            expect(page.get_by_role("dialog")).to_contain_text("추가매수 2주")
            assert not wire
            page.get_by_role("button", name="취소", exact=True).click()
            assert not wire
            page.get_by_role("button", name="보존 종목 추가매수", exact=True).click()
            page.get_by_role("button", name="실전 주문 전송", exact=True).click()
            expect(page.get_by_role("dialog")).to_have_count(0)
            assert wire == [("buy", 2, 110)]
            page.get_by_role("button", name="증권사 체결 확인 · 주문 전송 없음", exact=True).click()
            expect(row).to_contain_text("보유 7 주")
            page.get_by_role("button", name="보존 종목 일부매도", exact=True).click()
            expect(page.get_by_role("dialog")).to_contain_text("일부매도 2주")
            page.get_by_role("button", name="실전 주문 전송", exact=True).click()
            expect(page.get_by_role("dialog")).to_have_count(0)
            assert wire[-1] == ("sell", 2, None)
            page.get_by_role("button", name="증권사 체결 확인 · 주문 전송 없음", exact=True).click()
            expect(row).to_contain_text("보유 5 주")
            with page.expect_response(
                lambda r: r.url.endswith(f"/plans/{pid}/settings") and r.request.method == "PATCH"
            ):
                row.get_by_label("보존 종목 매매 수량", exact=True).fill("3")
                row.get_by_label("보존 종목 매매 수량", exact=True).blur()
            page.reload()
            page.get_by_role("button", name="종목 매매", exact=True).click()
            expect(row.get_by_label("보존 종목 매매 수량", exact=True)).to_have_value("3")
            expect(row.get_by_label("보존 종목 자동매수 가격 조건", exact=True)).to_have_value("at_or_above")
            expect(row.get_by_label("보존 종목 추가매수 지정가격", exact=True)).to_have_value("110")
            page.screenshot(path=f"/private/tmp/nastack-position-adjustment-{role}.png")
            page.set_viewport_size({"width": 390, "height": 844})
            page.get_by_role("button", name="메뉴 닫기", exact=True).click()
            page.locator("#rightRail").evaluate("rail=>rail.classList.add('collapsed')")
            page.wait_for_function("document.querySelector('#leftRail').getBoundingClientRect().right<=0 && document.querySelector('#rightRail').getBoundingClientRect().left>=innerWidth")
            row.locator(".mobile-trade-details").evaluate_all("items=>items.forEach(item=>item.open=true)")
            expect(row.get_by_label("보존 종목 자동매수 가격 조건", exact=True)).to_be_visible()
            assert row.locator(".trade-setting-box").evaluate_all("items=>items.every(item=>item.scrollWidth<=item.clientWidth+1)")
            row.get_by_label("보존 종목 자동매수 가격 조건", exact=True).scroll_into_view_if_needed()
            page.screenshot(path=f"/private/tmp/nastack-trade-price-boxes-mobile-{role}.png")
            page.set_viewport_size({"width": 1700, "height": 1100})
            page.locator("#leftRail").evaluate("rail=>rail.classList.remove('collapsed')")
            context.add_cookies([{"name": "trading_session", "value": viewer_token, "url": origin}])
            page.reload()
            page.get_by_role("button", name="종목 매매", exact=True).click()
            expect(page.get_by_role("button", name="조회전용 종목 추가매수", exact=True)).to_be_disabled()
            expect(page.get_by_role("button", name="조회전용 종목 일부매도", exact=True)).to_be_disabled()
            expect(page.get_by_label("조회전용 종목 자동매수 가격 조건", exact=True)).to_be_disabled()
            expect(page.get_by_label("조회전용 종목 추가매수 지정가격", exact=True)).to_be_disabled()
            expect(row).to_have_count(0)
            assert len(wire) == 2 and not errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
