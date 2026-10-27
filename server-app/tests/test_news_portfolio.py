import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_storage_news import clock
from test_vectors import seed_vector_data

from nastack_trading.app import create_app


def test_manual_profile_business_fallback_without_network():
    from nastack_trading.business_context import resolve_business

    context = resolve_business({"code": "108490", "name": "로보티즈"}, None)
    assert context["description"] and context["sources"]
    assert resolve_business({"code": "unknown", "name": "미확인"}, None) is None


@pytest.mark.parametrize("assessment_relevance", ["direct", "uncertain"])
def test_portfolio_disclosure_collection_and_separate_llm_score(
    admin, setup, monkeypatch, assessment_relevance
):
    seed_vector_data(setup[1])
    from nastack_trading.news import stock_context

    def with_business(*args, **kwargs):
        context = stock_context(*args, **kwargs)
        if context:
            context["business_context"] = "검토된 사업 설명"
        return context

    monkeypatch.setattr("nastack_trading.news.stock_context", with_business)
    monkeypatch.setattr("nastack_trading.news.now", clock)
    calls = []

    def respond(request):
        calls.append(request.method)
        if request.method == "GET":
            assert request.url.host == "kind.krx.co.kr"
            return httpx.Response(
                200,
                content=(
                    """<rss><channel>
                <item><title>합성 종목 잠정실적</title><link>https://kind.krx.co.kr/common/disclsviewer.do?acptNo=1</link><pubDate>Tue, 20 Oct 2026 17:00:00 GMT</pubDate></item>
                <item><title>다른 회사 잠정실적</title><link>https://kind.krx.co.kr/common/disclsviewer.do?acptNo=2</link><pubDate>Tue, 20 Oct 2026 17:00:00 GMT</pubDate></item>
                </channel></rss>"""
                ).encode(),
            )
        body = json.loads(json.loads(request.content)["messages"][1]["content"])
        article = body["articles"][0]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "assessments": [
                                        {
                                            "article_id": article["id"],
                                            "dimension_id": "stock-123456",
                                            "score9": None,
                                            "relevance": assessment_relevance,
                                            "reason": "제목만 있어 수치와 영향 방향 판단 불가",
                                            "evidence": "합성 종목 잠정실적",
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    config = setup[0].model_copy(
        update={"news_enabled": True, "vllm_base_url": "http://model.test/v1", "vllm_model_id": "base-model"}
    )
    with TestClient(
        create_app(config, news_transport=httpx.MockTransport(respond)),
        headers={"origin": "http://testserver"},
        cookies=admin.cookies,
    ) as client:
        result = client.post("/api/news/collect", json={"portfolio": True, "collect_only": True})
        assert result.status_code == 200, result.text
        assert calls == ["GET"]
        data = client.get("/api/news/today?code=123456").json()
        assert data["stock_context"] == "검토된 사업 설명"
        assert data["material_counts"]["reviewed"] == 0
        assert len(data["articles"]) == 1 and data["articles"][0]["content_type"] == "disclosure"
        assert data["coverage"]["complete"] is False and data["start_date"] == "2026-10-08"
        assert data["total_sum"] is None and data["article_impacts"][0]["assessment"] is None
        source = next(s for s in data["sources"] if s["domain"] == "kind.krx.co.kr")
        result = client.post("/api/news/analyze", json={"source_ids": [source["id"]], "code": "123456"})
        assert result.status_code == 200, result.text
        data = client.get("/api/news/today?code=123456").json()
        assert calls == ["GET", "POST"]
        assert data["material_counts"]["reviewed"] == 1
        assert len(data["article_impacts"]) == 1
        assert data["article_impacts"][0]["assessment"]["relevance"] == assessment_relevance
        assert data["total_sum"] is None
        assert data["article_impacts"][0]["assessment"]["reason"] == "제목만 있어 수치와 영향 방향 판단 불가"
