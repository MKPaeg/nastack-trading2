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
            requests = []

            def research(route):
                if route.request.method == "POST":
                    requests.append(route.request.post_data_json)
                    records.clear()
                    route.fulfill(json={"status": "completed"})
                else:
                    route.fulfill(json={"items": records, "context": None})

            page.route("**/api/business-research*", research)
            page.goto(origin)
            page.get_by_role("button", name="설정", exact=True).click()
            choice = page.get_by_label("사업 설명 조사 종목")
            expect(choice).to_be_visible(timeout=30000)
            expect(page.get_by_text("OpenAI 모델 HTTP 429" if failure_code == "PROVIDER_HTTP_ERROR" else "모델의 인용 문장이 제공 원문과 일치하지 않습니다", exact=False)).to_be_visible()
            expect(page.get_by_text("사용 토큰 123", exact=False)).to_be_visible()
            expect(page.get_by_text("검증 항목: claims.0.quote · string_too_long · 최대 200 · 실제 201", exact=True)).to_be_visible()
            retry = page.get_by_role("button", name="실패한 사업설명 재처리", exact=True)
            expect(retry).to_be_enabled()
            page.once("dialog", lambda dialog: dialog.accept())
            retry.click()
            expect(page.get_by_text("사업 설명을 개인 종목 속성에 저장했습니다.", exact=False)).to_be_visible()
            assert requests[0]["retry_of"] == "synthetic-failed-record"
            assert requests[0]["confirmed"] is True and requests[0]["code"] == "123456"
            page.screenshot(path="/private/tmp/nastack-business-retry-test.png", full_page=True)
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
