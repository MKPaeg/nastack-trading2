import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from pydantic import SecretStr
from sqlalchemy import select
from test_business_research import business_client  # noqa: F401

from nastack_trading.app import create_app
from nastack_trading.models import User
from nastack_trading.security import create_session


@pytest.mark.parametrize("failure_code", ["BUSINESS_INVALID_EVIDENCE", "PROVIDER_HTTP_ERROR"])
def test_failed_business_ui_displays_reason_and_explicit_retry(business_client, setup, failure_code):  # noqa: F811
    with setup[1].begin() as db:
        token = create_session(db, db.scalar(select(User)), 24)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    cfg = setup[0].model_copy(update={"allowed_origins": [origin], "live_worker_enabled": False,
        "vllm_base_url": "http://model.test/v1", "vllm_model_id": "test-model", "tavily_api_key": SecretStr("synthetic")})
    offline = httpx.MockTransport(lambda request: httpx.Response(503))
    server = uvicorn.Server(uvicorn.Config(create_app(cfg, news_transport=offline, broker_transport=offline),
        host="127.0.0.1", port=port, log_level="error"))
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
            records = [{"id": "synthetic-failed-record", "code": "123456", "status": "failed",
                "created_at": "2026-10-26T17:00:00+09:00", "error": failure_code,
                "failure_message": "OpenAI 모델 HTTP 429 · API 사용 한도를 확인하세요" if failure_code == "PROVIDER_HTTP_ERROR" else "모델의 인용 문장이 제공 원문과 일치하지 않습니다",
                "validation_issues": [{"field": "claims.0.quote", "type": "string_too_long", "max_length": 200, "actual_length": 201}],
                "usage": {"total_tokens": 123}, "search_reused": True}]
            result_state = {"context": None}
            usage = {"llm_calls": 1, "known": True, "total_tokens": 67, "input_tokens": 62,
                     "output_tokens": 5, "cached_input_tokens": 0, "cache_write_known": True,
                     "cache_write_tokens": 0, "cost_known": True, "priced_calls": 1,
                     "estimated_usd": 0.0000184, "pricing_as_of": "2026-10-27"}

            def research(route):
                assert route.request.method == "GET"
                route.fulfill(json={"items": records, "context": result_state["context"]})

            page.route("**/api/business-research*", research)
            page.goto(origin)
            page.get_by_role("button", name="사용자 설정", exact=True).click()
            expect(page.get_by_text("종목 사업 설명 재조사", exact=True)).to_have_count(0)
            page.get_by_role("button", name="데이터 수집", exact=True).click()
            page.get_by_role("tab", name="사업 설명 재조사", exact=True).click()
            choice = page.get_by_label("사업 설명 재조사 종목")
            expect(choice).to_be_visible(timeout=30000)
            expect(page.get_by_text("뉴스의 호재·악재 점수를 다시 평가하는 기능은 아닙니다.", exact=False)).to_be_visible()
            expect(page.get_by_text("OpenAI 모델 HTTP 429" if failure_code == "PROVIDER_HTTP_ERROR" else "모델의 인용 문장이 제공 원문과 일치하지 않습니다", exact=False)).to_be_visible()
            expect(page.get_by_text("사용 토큰 123", exact=False)).to_be_visible()
            expect(page.get_by_text("검증 항목: claims.0.quote · string_too_long · 최대 200 · 실제 201", exact=True)).to_be_visible()
            retry = page.get_by_role("button", name="실패한 사업 설명 재조사 재시도", exact=True)
            expect(retry).to_be_enabled()
            reload = page.get_by_role("button", name="저장 설명·조사 이력 조회", exact=True)
            retry_box, reload_box = retry.bounding_box(), reload.bounding_box()
            assert retry_box and reload_box and reload_box["x"] - (retry_box["x"] + retry_box["width"]) >= 12
            page.evaluate("""usage => {
                const original = window.fetch.bind(window);
                window.fetch = (url, options = {}) => {
                    if (String(url) === '/api/business-research' && options.method === 'POST') {
                        window.__businessRequest = JSON.parse(options.body);
                        return new Promise(resolve => {
                            window.__finishBusinessResearch = () => resolve(new Response(
                                JSON.stringify({status: 'completed'}),
                                {status: 200, headers: {'Content-Type': 'application/json', 'X-LLM-Usage': usage}}
                            ));
                        });
                    }
                    return original(url, options);
                };
            }""", json.dumps(usage))
            page.once("dialog", lambda dialog: dialog.accept())
            retry.click()
            progress = page.locator(".business-research-progress")
            expect(progress).to_be_visible()
            expect(progress).to_contain_text("웹 검색·사업 설명 재조사 중…")
            expect(progress).to_contain_text("사용량 집계 대기")
            expect(progress).to_have_attribute("aria-busy", "true")
            records[:] = [{"id": "synthetic-completed-record", "code": "123456", "status": "completed",
                           "created_at": "2026-10-28T01:29:17+09:00"}] + [
                               {"id": f"synthetic-history-{index}", "code": "123456", "status": "completed",
                                "created_at": f"2026-10-{27-index:02d}T02:07:30+09:00"}
                               for index in range(12)
                           ]
            result_state["context"] = {"description": "근거를 검증한 새 사업 설명", "topics": ["검사장비"], "source_kind": "llm_web_evidence"}
            page.evaluate("() => window.__finishBusinessResearch()")
            expect(page.get_by_text("사업 설명을 개인 종목 속성에 저장했습니다.", exact=False)).to_be_visible()
            expect(progress).to_contain_text("사용 토큰 67개")
            expect(progress).to_contain_text("예상 비용 US$0.000018")
            expect(progress).to_have_attribute("aria-busy", "false")
            expect(page.get_by_role("button", name="사업 설명 재조사", exact=True)).to_be_visible()
            history_list = page.get_by_role("region", name="사업 설명 조사 이력 목록")
            expect(history_list.locator(".business-research-history-entry")).to_have_count(13)
            assert history_list.evaluate("el => getComputedStyle(el).overflowY") == "auto"
            assert history_list.evaluate("el => el.scrollHeight > el.clientHeight")
            completed_date = page.locator(".business-research-date").first
            expect(completed_date).to_contain_text("조사 완료")
            expect(completed_date).to_contain_text("2026. 10. 28.")
            assert completed_date.evaluate("el => getComputedStyle(el).borderLeftWidth") == "4px"
            request = page.evaluate("() => window.__businessRequest")
            assert request["retry_of"] == "synthetic-failed-record"
            assert request["confirmed"] is True and request["code"] == "123456"
            page.screenshot(path="/private/tmp/nastack-business-retry-test.png", full_page=True)
            page.set_viewport_size({"width": 600, "height": 900})
            assert page.locator(".business-research-actions").evaluate(
                "element => element.scrollWidth <= element.clientWidth"
            )
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
