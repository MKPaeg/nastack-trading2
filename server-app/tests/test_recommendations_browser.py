import socket
import threading
import time

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from test_kiwoom import configuration

from nastack_trading.app import create_app
from nastack_trading.kiwoom import KiwoomReader
from nastack_trading.models import Instrument, KiwoomConnection, User, UserSettings
from nastack_trading.security import create_session


def test_recommendations_ui_ordering_refresh_restore_and_viewer(setup, monkeypatch):
    from nastack_trading import market

    calls = []
    session_state = {"is_open": True, "reason": "정규장 운영 중"}
    monkeypatch.setattr(market, "market_session", lambda: session_state)
    monkeypatch.setattr(KiwoomReader, "token", lambda *args: ("synthetic", "live"))

    def read(self, token, environment, api_id, payload, continuation=None):
        assert api_id == "ka10027"
        calls.append(payload["sort_tp"])
        return {"pred_pre_flu_rt_upper": [{"stk_cd": "005930", "stk_nm": "순위시험종목",
            "cur_prc": "10000", "pred_pre": "500", "flu_rt": "5", "now_trde_qty": "1000",
            "pred_pre_sig": "2" if payload["sort_tp"] == "1" else "5"}]}, {}

    monkeypatch.setattr(KiwoomReader, "read", read)
    with setup[1].begin() as db:
        user = db.scalar(select(User))
        token = create_session(db, user, 24)
        # A pre-upgrade end date must not hide today's bar on recommendation entry.
        settings = db.get(UserSettings, user.id)
        settings.value = {**settings.value, "default_attributes_enabled": False, "screen_state": {
            "view": "dashboard", "chart_end": {"005930": "2026-10-25", "328130": "2026-10-22"}}}
        db.add(Instrument(tenant_id=user.tenant_id, code="005930", value={
            "name": "순위시험종목", "status": "active", "market": "KOSPI", "asset_type": "stock"}))
        db.add(KiwoomConnection(owner_id=user.id, tenant_id=user.tenant_id, enabled=True, environment="live", version=1))
        viewer = User(tenant_id=user.tenant_id, email="rank-viewer@example.com", role="viewer", active=True, code_hash="unused")
        db.add(viewer)
        db.flush()
        db.add(UserSettings(user_id=viewer.id, tenant_id=viewer.tenant_id, value={}))
        viewer_token = create_session(db, viewer, 24)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    config = configuration(setup[0]).model_copy(update={"allowed_origins": [origin], "live_worker_enabled": False})
    offline = httpx.MockTransport(lambda request: httpx.Response(503))
    server = uvicorn.Server(uvicorn.Config(create_app(config, broker_transport=offline, news_transport=offline), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.add_cookies([{"name": "trading_session", "value": token, "url": origin}])
            page = context.new_page()
            page.route("**/api/market/session", lambda route: route.fulfill(json=session_state))
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin)
            nav = page.locator(".nav-item[data-view]").evaluate_all("items=>items.map(i=>i.dataset.view)")
            assert nav.index("recommendations") == nav.index("watchlist") + 1
            page.get_by_role("button", name="추천종목", exact=True).click()
            button = page.get_by_role("button", name="현재 순위 조회", exact=True)
            expect(button).to_be_enabled()
            assert calls == []
            button.click()
            expect(page.get_by_text("순위시험종목 · 005930", exact=True)).to_have_count(2)
            expect(page.get_by_role("button", name="순위시험종목 관심종목 추가", exact=True)).to_have_count(2)
            assert calls == ["1", "3"]
            expect(button).to_be_enabled()
            page.evaluate("""() => {
                window.automaticButtonMutations = [];
                const button = [...document.querySelectorAll('button')].find(b => b.textContent === '현재 순위 조회');
                new MutationObserver(records => window.automaticButtonMutations.push(...records.map(r => r.type)))
                    .observe(button, {childList:true, subtree:true, attributes:true, attributeFilter:['class','disabled','aria-busy']});
            }""")
            page.wait_for_timeout(3500)
            assert calls == ["1", "3", "1", "3"]
            expect(button).to_have_text("현재 순위 조회")
            expect(button).to_be_enabled()
            assert page.evaluate("window.automaticButtonMutations") == []
            session_state.update(is_open=False, reason="정규장 외 시간")
            page.wait_for_timeout(3500)
            assert calls == ["1", "3", "1", "3"]
            expect(page.get_by_text("자동 갱신 중지 · 정규장 외 시간 · 저장 결과 표시 · 수동 조회 가능", exact=True)).to_be_visible()
            expect(button).to_be_enabled()
            button.click()
            expect(button).to_be_enabled()
            assert calls == ["1", "3", "1", "3", "1", "3"]
            session_state.update(is_open=True, reason="정규장 운영 중")
            page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true})")
            page.wait_for_timeout(3500)
            assert calls == ["1", "3", "1", "3", "1", "3"]
            page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false})")
            page.get_by_label("추천종목 시장").select_option("101")
            page.wait_for_timeout(900)
            page.reload()
            expect(page.get_by_label("추천종목 시장")).to_have_value("101")
            assert calls == ["1", "3", "1", "3", "1", "3"]
            page.get_by_label("추천종목 시장").select_option("000")
            expect(page.get_by_role("button", name="순위시험종목 · 005930", exact=True)).to_have_count(2)
            trace, initialized, workflow_retries = [], {"ready": False}, []

            def defaults(route):
                if route.request.method == "POST":
                    trace.append("defaults")
                    initialized["ready"] = True
                route.fulfill(json={"profile": {"status": "ready", "selected_dimensions": ["sector-01"]} if initialized["ready"] else None})

            def counted(label, value):
                def respond(route):
                    trace.append(label)
                    if label == "workflow":
                        assert route.request.post_data_json["mode"] == "all"
                        workflow_retries.append(route.request.post_data_json["retry_failed"])
                    route.fulfill(json=value)
                return respond

            page.route("**/api/vectors/defaults/005930", defaults)
            page.route("**/api/business-research?code=005930", lambda route: route.fulfill(json={"context": {
                "description": "합성 사업설명: 반도체 설계와 전자 제품을 공급합니다.",
                "sources": ["https://example.com/business"]}, "items": []}))
            page.route("**/api/recommendations/005930/initialize", defaults)
            page.route("**/api/vectors/stock-history", counted("history", {"mode": "cached", "chart": [], "remaining": []}))
            page.route("**/api/vectors/preview", lambda route: route.fulfill(json={"cache_hit": False}))
            page.route("**/api/vectors/analyze", counted("vector", {"status": "completed", "persistence": {"id": "synthetic-result"}, "comparison": []}))
            page.route("**/api/vectors/attributes", counted("assign", {"origin": "recommendation_initial"}))
            page.route("**/api/news/models", lambda route: route.fulfill(json={"selected": "default", "default": {"provider": "vllm", "model": "synthetic"}, "items": []}))
            page.route("**/api/news/workflow/005930/refresh", counted("workflow", {"status": "completed"}))
            page.route("**/api/news/workflow/005930", lambda route: route.fulfill(json={"status": "completed"}))
            page.route("**/api/news/today?code=005930*", lambda route: route.fulfill(json={"total_sum": 2, "material_counts": {"reviewed": 1, "scored": 1}, "analysis_pool_count": 1}))
            page.route("**/api/market/instruments/005930/refresh*", lambda route: route.fulfill(json={"snapshot": None}))
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="순위시험종목 · 005930", exact=True).first.click()
            expect(page.locator("#liveContent").get_by_role("heading", name="종목 상세", exact=True)).to_be_visible()
            expect(page.get_by_text("최초 트레이딩 벡터 할당 완료 · 기존 수동 속성은 보존", exact=True)).to_be_visible(timeout=30000)
            expect(page.get_by_text("+2점", exact=True)).to_be_visible()
            expect(page.locator(".detail-analysis-summary .business-description-text")).to_have_text("합성 사업설명: 반도체 설계와 전자 제품을 공급합니다.")
            expect(page.get_by_role("button", name="순위시험종목 관심종목 추가", exact=True)).to_have_count(1)
            assert page.locator(".saved-business-description").evaluate("el=>!!(el.compareDocumentPosition([...el.parentNode.children].find(x=>x.tagName==='BUTTON')) & Node.DOCUMENT_POSITION_FOLLOWING)")
            today = page.evaluate("new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())")
            expect(page.get_by_label("차트 종료일", exact=True)).to_have_value(today)
            assert trace == ["defaults", "history", "vector", "assign", "workflow"]
            retry = page.get_by_role("button", name="추천종목 초기 분석 확인·재시도", exact=True)
            expect(retry).to_be_enabled()
            assert retry.evaluate("el=>getComputedStyle(el).backgroundColor") == "rgb(255, 214, 90)"
            page.once("dialog", lambda dialog: dialog.accept())
            retry.click()
            expect(retry).to_be_enabled(timeout=10000)
            assert workflow_retries == [False, True]
            assert page.request.get(origin + "/api/watchlist").json()["total"] == 0
            # A manual date still works within the detail screen.
            end = page.get_by_label("차트 종료일", exact=True)
            end.fill("2026-10-23")
            end.dispatch_event("change")
            expect(end).to_have_value("2026-10-23")
            # Returning from the current ranking explicitly resets this symbol only.
            page.get_by_role("button", name="추천종목", exact=True).click()
            page.get_by_role("button", name="순위시험종목 · 005930", exact=True).first.click()
            expect(page.get_by_label("차트 종료일", exact=True)).to_have_value(today)
            page.wait_for_timeout(900)
            screen = page.request.get(origin + "/api/settings").json()["value"]["screen_state"]
            assert "005930" not in screen["chart_end"]
            assert screen["chart_end"]["328130"] == "2026-10-22"
            page.screenshot(path="/private/tmp/nastack-recommendation-chart-today.png", full_page=True)
            page.route("**/api/vectors/instruments", lambda route: route.fulfill(json={"items": [
                {"code": "005930", "name": "순위시험종목", "market": "KRX", "asset_type": "stock"}]}))
            page.get_by_role("tab", name="트레이딩 벡터", exact=True).click()
            expect(page.locator("#vectorAnalysis .business-description-text")).to_have_text("합성 사업설명: 반도체 설계와 전자 제품을 공급합니다.")
            expect(page.get_by_role("button", name="순위시험종목 관심종목 추가", exact=True)).to_have_count(1)
            page.screenshot(path="/private/tmp/nastack-vector-business.png", full_page=True)
            long_business = "제품과 고객, 시장에 관한 저장된 사업설명입니다.\n" * 35
            page.route("**/api/news/today?code=005930*", lambda route: route.fulfill(json={
                "enabled": True, "local_model_configured": True, "levels": 5,
                "date": "2026-10-26", "start_date": "2026-10-12", "end_date": "2026-10-26", "timezone": "Asia/Seoul",
                "instrument": {"name": "순위시험종목", "code": "005930", "business_context": long_business},
                "total_sum": 0, "sources": [], "articles": [], "aggregate": [], "assessments": [], "runs": []}))
            # News requests place levels before code; use an equivalent route for that order.
            page.route("**/api/news/today?levels=5&code=005930", lambda route: route.fulfill(json={
                "enabled": True, "local_model_configured": True, "levels": 5,
                "date": "2026-10-26", "timezone": "Asia/Seoul", "instrument": {
                    "name": "순위시험종목", "code": "005930", "business_context": long_business},
                "total_sum": 0, "sources": [], "articles": [], "aggregate": [], "assessments": [], "runs": []}))
            page.get_by_role("tab", name="뉴스 및 통계 분석", exact=True).click()
            business = page.get_by_label("뉴스 분석 종목 사업 정보", exact=True)
            expect(business).to_have_value(long_business)
            page.wait_for_timeout(100)
            assert business.evaluate("el=>el.clientHeight>=el.scrollHeight")
            assert page.locator(".news-model-panel .news-financials").evaluate("el=>getComputedStyle(el).borderTopWidth") == "0px"
            add = page.locator(".news-stock-row .watchlist-add-action button")
            expect(add).to_be_visible()
            page.screenshot(path="/private/tmp/nastack-news-business.png", full_page=True)
            add.click()
            expect(page.locator(".news-stock-row .watchlist-add-action")).to_have_text("관심종목에 추가됨")
            saved_watch = page.request.get(origin + "/api/watchlist").json()
            assert saved_watch["total"] == 1 and saved_watch["items"][0]["instrument"]["code"] == "005930"
            page.get_by_role("button", name="추천종목", exact=True).click()
            expect(page.get_by_role("button", name="순위시험종목 관심종목 추가", exact=True)).to_have_count(0)
            page.screenshot(path="/private/tmp/nastack-recommendations-test.png", full_page=True)
            context.add_cookies([{"name": "trading_session", "value": viewer_token, "url": origin}])
            page.reload()
            page.get_by_role("button", name="추천종목", exact=True).click()
            expect(page.get_by_label("추천종목 시장")).to_have_value("000")
            expect(page.get_by_role("button", name="현재 순위 조회", exact=True)).to_be_disabled()
            expect(page.get_by_text("순위시험종목 · 005930", exact=True)).to_have_count(0)
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
