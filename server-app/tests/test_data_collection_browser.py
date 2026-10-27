"""Real browser against a disposable DB; no live server, keys or paid calls."""

import json
import socket
import threading
import time
from datetime import date

import httpx
import uvicorn
from cryptography.fernet import Fernet
from pydantic import SecretStr
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select

from nastack_trading.app import create_app
from nastack_trading.models import User, UserSettings, DimensionCollection
from nastack_trading.schemas import Preferences
from nastack_trading.security import create_session


def test_data_collection_screen_save_reload_roles_and_no_paid_calls(setup, tmp_path):
    config, factory = setup
    with factory.begin() as db:
        user = db.scalar(select(User))
        token = create_session(db, user, 24)
        viewer = User(
            tenant_id=user.tenant_id,
            email="data-viewer@example.com",
            role="viewer",
            active=True,
            code_hash="unused",
        )
        db.add(viewer)
        db.flush()
        db.add(
            UserSettings(
                user_id=viewer.id, tenant_id=viewer.tenant_id, value=Preferences().model_dump(mode="json")
            )
        )
        viewer_token = create_session(db, viewer, 24)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    config = config.model_copy(
        update={
            "allowed_origins": [origin],
            "live_orders_enabled": False,
            "automatic_orders_enabled": False,
            "live_worker_enabled": False,
            "broker_encryption_key": SecretStr(Fernet.generate_key().decode()),
        }
    )
    krx_calls = []
    def synthetic_krx(request):
        assert request.url.host == "data-dbg.krx.co.kr"
        krx_calls.append(request.url.params["basDd"])
        return httpx.Response(200, json={"OutBlock_1": [{"BAS_DD": krx_calls[-1], "IDX_NM": "VKOSPI", "CLSPRC_IDX": "23.45"}]})
    server = uvicorn.Server(
        uvicorn.Config(create_app(config, dimension_transport=httpx.MockTransport(synthetic_krx)), host="127.0.0.1", port=port, log_level="error")
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
            mutations, errors = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "request", lambda request: mutations.append(request.url) if request.method == "POST" else None
            )
            page.goto(origin)
            page.get_by_role("button", name="데이터 수집", exact=True).click()
            expect(page.get_by_role("heading", name="화면과 독립적인 원천 갱신")).to_be_visible()
            expect(page.get_by_label("뉴스·공시 자동 수집 허용")).not_to_be_checked()
            expect(
                page.get_by_text("옵션을 변경한 뒤 ‘내 수집 설정 저장’을 눌러야 반영됩니다.", exact=False)
            ).to_be_visible()
            toggles = page.locator(".collection-toggle")
            expect(toggles).to_have_count(4)
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 1000})
                layout = toggles.evaluate_all("""items => items.map(label => {
                  const input=label.querySelector('input'),text=label.querySelector('span');
                  const a=input.getBoundingClientRect(),b=text.getBoundingClientRect();
                  return {first:label.firstElementChild===input,left:a.right<=b.left,visible:a.width>0&&b.width>0};
                })""")
                assert all(item["first"] and item["left"] and item["visible"] for item in layout)
            page.set_viewport_size({"width": 1440, "height": 1000})
            # Slow responses exercise real loading/disabled states without any provider calls.
            fetch_script = """(() => {
              const original=window.fetch;
              window.collectionTestDelay=null;
              window.fetch=async function(url,options={}) {
                const test=window.collectionTestDelay;
                if(test && String(url).includes(test.path) && (options.method||'GET')===test.method) {
                  await new Promise(resolve=>setTimeout(resolve,900));
                  if(test.fail)return new Response(JSON.stringify({detail:'검증용 조회 실패'}),{status:500,headers:{'Content-Type':'application/json'}});
                }
                return original.call(this,url,options);
              };
            })();"""
            page.add_init_script(fetch_script)
            page.evaluate(fetch_script)
            page.evaluate("window.collectionTestDelay={path:'/api/data-collection',method:'PUT'}")
            page.get_by_label("일일 검색 요청 한도").fill("3")
            interval = page.get_by_label("뉴스 점검 간격 (1~168시간)")
            expect(interval).to_have_attribute("min", "1")
            expect(interval).to_have_attribute("step", "1")
            interval.fill("0")
            page.get_by_role("button", name="내 수집 설정 저장").click()
            expect(page.get_by_text("뉴스 점검 간격은 1~168시간 사이의 정수로 입력하세요.")).to_be_visible()
            interval.fill("1")
            # Changing an option alone must not save it or start a collection.
            assert (
                page.evaluate(
                    "async()=>{const r=await fetch('/api/data-collection');return (await r.json()).settings.search_daily_limit;}"
                )
                == 10
            )
            with page.expect_response(
                lambda response: (
                    response.url.endswith("/api/data-collection") and response.request.method == "PUT"
                )
            ) as saved:
                page.get_by_role("button", name="내 수집 설정 저장").click()
                saving = page.get_by_role("button", name="수집 설정 저장 중…")
                expect(saving).to_be_disabled()
                expect(saving).to_have_attribute("aria-busy", "true")
                expect(saving).to_have_class("primary task-busy")
            assert saved.value.status == 200
            expect(page.get_by_label("일일 검색 요청 한도")).to_have_value("3")
            expect(page.get_by_label("뉴스 점검 간격 (1~168시간)")).to_have_value("1")
            page.wait_for_timeout(900)  # Screen selection persistence is debounced 600 ms.
            assert (
                page.evaluate(
                    "async()=>{const r=await fetch('/api/settings');const d=await r.json();return d.value?.screen_state?.view;}"
                )
                == "data"
            )
            page.reload()
            expect(page.get_by_role("heading", name="화면과 독립적인 원천 갱신")).to_be_visible()
            expect(page.get_by_label("일일 검색 요청 한도")).to_have_value("3")
            expect(page.get_by_label("뉴스 점검 간격 (1~168시간)")).to_have_value("1")
            page.get_by_role("tab", name="다차원 데이터", exact=True).click()
            expect(page.get_by_role("heading", name="다차원 데이터 · 원천 수집 점검")).to_be_visible()
            # New public original-index connector is present in the actual UI;
            # opening the screen must not collect or send user credentials.
            available = page.evaluate("async()=>{const r=await fetch('/api/dimensions');return (await r.json()).items;}")
            taiex = next(item for item in available if item["id"] == "market-15")
            assert taiex["can_collect"] and taiex["provider"] == "twse_taiex"
            # A verified, still-uncollected VKOSPI must appear above a saved
            # KOSPI even when the user's saved-only filter remains enabled.
            with factory.begin() as db:
                owner = db.scalar(select(User).where(User.role == "admin"))
                db.add(DimensionCollection(owner_id=owner.id, tenant_id=owner.tenant_id, dimension_id="market-01",
                    start_date=date(2026,1,1), end_date=date(2026,1,6), status="completed",
                    value={"provider":"namuh_etf","series_code":"226490","points":[{"date":"2026-01-02","value":12000}]}))
            verified = page.evaluate("""async()=>{
                await api('/user/connections/krx',{method:'PUT',body:JSON.stringify({version:0,enabled:true,fields:{api_key:'synthetic-browser-krx-key'}})});
                return await api('/user/connections/krx/test',{method:'POST'});
            }""")
            assert verified["status"] == "connected" and len(krx_calls) == 1
            page.reload()
            page.get_by_label("차원 유형", exact=True).select_option("market")
            page.get_by_label("수집된 자료만 보기").check()
            ids = page.get_by_label("다차원 데이터 선택", exact=True).evaluate("e=>[...e.options].map(o=>o.value)")
            assert ids[:2] == ["control-11", "market-01"]
            page.get_by_label("다차원 데이터 선택", exact=True).select_option("control-11")
            expect(page.get_by_role("button", name="수동 수집", exact=True)).to_be_enabled()
            expect(page.locator("#dimensionSeries")).to_contain_text("VKOSPI")
            page.screenshot(path="/private/tmp/nastack-vkospi-market.png", full_page=True)
            assert len(krx_calls) == 1  # Screen entry did not start a provider read.
            page.get_by_label("수집된 자료만 보기").uncheck()
            page.get_by_label("차원 유형", exact=True).select_option("all")
            page.get_by_label("다차원 데이터 선택", exact=True).select_option("market-15")
            expect(page.get_by_label("다차원 데이터 선택", exact=True)).to_have_value("market-15")
            expect(page.locator('option[value="market-15"]')).to_contain_text("구현 확인")
            page.get_by_label("다차원 데이터 선택", exact=True).select_option("commodity-06")
            expect(page.get_by_label("거래일 표시", exact=True)).to_be_disabled()
            expect(page.locator("#dimensionSeries")).to_contain_text("월간 자료 · 적용 안 함")
            expect(page.locator("#dimensionSeries")).to_contain_text("일별 벡터/예측 입력")
            expect(page.locator('option[value="commodity-06"]')).to_contain_text("월간 수집 구현")
            page.screenshot(path="/private/tmp/nastack-monthly-dimension.png", full_page=True)
            for key, label in [("commodity-11", "IMF"), ("commodity-29", "2026 이후"), ("commodity-31", "Arabica"), ("commodity-33", "RSS3")]:
                page.get_by_label("다차원 데이터 선택", exact=True).select_option(key)
                expect(page.locator(f'option[value="{key}"]')).to_contain_text(label)
                expect(page.locator(f'option[value="{key}"]')).to_contain_text("월간 수집 구현")
                expect(page.get_by_label("거래일 표시", exact=True)).to_be_disabled()
                expect(page.get_by_role("button", name="수동 수집", exact=True)).to_be_enabled()
            page.wait_for_timeout(900)
            page.reload()
            expect(page.get_by_label("다차원 데이터 선택", exact=True)).to_have_value("commodity-33")
            expect(page.get_by_label("거래일 표시", exact=True)).to_be_disabled()
            page.screenshot(path="/private/tmp/nastack-rss3-monthly.png", full_page=True)
            page.get_by_label("다차원 데이터 선택", exact=True).select_option("market-15")
            expect(page.get_by_label("거래일 표시", exact=True)).to_be_enabled()
            read = page.get_by_role("button", name="저장 자료 조회", exact=True)
            expect(read).to_be_enabled()
            page.evaluate("window.collectionTestDelay={path:'/series?',method:'GET'}")
            read.click()
            expect(page.get_by_role("button", name="저장 자료 조회 중…")).to_have_attribute(
                "aria-busy", "true"
            )
            expect(page.get_by_role("button", name="수동 수집", exact=True)).to_be_disabled()
            expect(read).to_be_enabled()
            # Intercept manual collection in the browser: no collector/provider runs.
            page.evaluate("window.collectionTestDelay=null")
            manual = page.get_by_role("button", name="수동 수집", exact=True)
            def reuse(route):
                payload = route.request.post_data_json
                url = route.request.url.replace("/collect", "/series")
                series = page.request.get(url, params=payload).json()
                route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({**series, "sync": {"mode": "cached", "fetched": [], "remaining": []}}),
                )
            page.route("**/api/dimensions/*/collect", reuse)
            manual.click()
            expect(page.get_by_text("수동 수집 완료 ·", exact=False)).to_contain_text("LLM 사용 토큰 0개")
            expect(page.get_by_text("수동 수집 완료 ·", exact=False)).to_contain_text("외부 API 호출 없음")
            page.unroute("**/api/dimensions/*/collect", reuse)
            page.route("**/api/dimensions/*/collect", lambda route: route.fulfill(
                status=429, content_type="application/json", body=json.dumps({"detail": "오늘의 수집 시도 한도"})
            ))
            manual.click()
            expect(page.get_by_text("수동 수집 실패 ·", exact=False)).to_contain_text("LLM 사용 토큰 0개")
            expect(manual).to_be_enabled()
            page.get_by_role("tab", name="통계 데이터", exact=True).click()
            stat_reload = page.get_by_role("button", name="통계 DB 새로고침", exact=True)
            expect(stat_reload).to_be_enabled()
            page.evaluate("window.collectionTestDelay={path:'/api/statistics',method:'GET',fail:true}")
            stat_reload.click()
            expect(page.get_by_role("button", name="저장 통계 조회 중…")).to_be_disabled()
            expect(page.get_by_text("검증용 조회 실패", exact=True)).to_be_visible()
            expect(stat_reload).to_be_enabled()
            page.evaluate("window.collectionTestDelay=null")
            stat_reload.click()
            expect(stat_reload).to_be_enabled()
            page.get_by_role("tab", name="뉴스·공시 수집 계획", exact=True).click()
            expect(
                page.get_by_role("heading", name="뉴스·공시 수집 · 영향 평가는 종목 분석에서 별도 실행")
            ).to_be_visible()
            page.route("**/api/vectors/instruments", lambda route: route.fulfill(
                json={"items": [{"code": "123456", "name": "종목 A"}, {"code": "654321", "name": "종목 B"}]}
            ))
            page.route("**/api/news/workflow/*", lambda route: route.fulfill(json={"status": "missing"}))
            page.evaluate("""() => {
              window.batchTest={calls:[],active:0,max:0};
              window.refreshBusinessCollection=async(code,notify,retry,valid,mode)=>{
                const t=window.batchTest;t.calls.push({code,mode});t.active++;t.max=Math.max(t.max,t.active);
                await new Promise(resolve=>setTimeout(resolve,200));t.active--;
                return {status:'collected',collection_usage:{known:true,total_tokens:code==='123456'?7:13}};
              };
            }""")
            page.get_by_role("tab", name="갱신 상태·비용 한도", exact=True).click()
            page.get_by_role("tab", name="뉴스·공시 수집 계획", exact=True).click()
            each = page.get_by_role("button", name="계획 확인·부족 자료 수집 (평가 안 함)", exact=True)
            expect(each).to_have_count(2)
            page.once("dialog", lambda dialog: dialog.accept())
            each.first.click()
            expect(page.get_by_text("collected · LLM 사용 토큰 7개", exact=False)).to_be_visible()
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="모든 종목 자료 조회", exact=True).click()
            expect(page.get_by_text("처리 2/2 · 실패·미완료 0건 · LLM 사용 토큰 20개", exact=False)).to_be_visible()
            assert page.evaluate("window.batchTest.max") == 1
            assert page.evaluate("window.batchTest.calls.map(x=>x.mode)") == ["collect"] * 3
            token_note = page.evaluate("collectionTokenText([{reused:true,collection_usage:{known:true,total_tokens:99}},{}])")
            assert token_note.startswith("LLM 사용 토큰 0개 · 확인 불가 1건 (합계 제외) · 검색 API 비용 별도")
            assert "0원으로 간주하지 않음" in token_note
            page.get_by_role("tab", name="갱신 상태·비용 한도", exact=True).click()
            expect(page.get_by_label("일일 검색 요청 한도")).to_have_value("3")
            page.screenshot(path="/private/tmp/nastack-data-collection-screen.png", full_page=True)
            # Account switch clears state; the second user's defaults remain separate.
            context.add_cookies([{"name": "trading_session", "value": viewer_token, "url": origin}])
            page.reload()
            page.get_by_role("button", name="데이터 수집", exact=True).click()
            expect(page.get_by_label("일일 검색 요청 한도")).to_have_value("10")
            expect(page.get_by_role("button", name="내 수집 설정 저장")).to_be_disabled()
            page.get_by_role("tab", name="뉴스·공시 수집 계획", exact=True).click()
            expect(page.get_by_role("button", name="모든 종목 자료 조회", exact=True)).to_be_disabled()
            for button in page.get_by_role("button", name="계획 확인·부족 자료 수집 (평가 안 함)", exact=True).all():
                expect(button).to_be_disabled()
            assert sum("/collect" in url for url in mutations) == 2  # Both intercepted above.
            assert not any("/refresh" in url or "/news/workflow/" in url for url in mutations)
            assert errors == []
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
