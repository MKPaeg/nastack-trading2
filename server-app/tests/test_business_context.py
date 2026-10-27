import json

import httpx
from pydantic import SecretStr

from nastack_trading.business_context import resolve_business, suggested_ids
from nastack_trading.config import Config
from nastack_trading.dimension_catalog import DIMENSIONS


def test_reviewed_business_is_code_and_name_bound_and_routes_supported_topics():
    config = Config(_env_file=None)
    context = resolve_business({"code": "066570", "name": "LG전자", "asset_type": "stock"}, config)
    assert context["sources"] and "가전" in context["description"]
    assert "sector-37" in suggested_ids(context, DIMENSIONS, "KOSPI", "066570")
    assert resolve_business({"code": "066570", "name": "다른 회사"}, config) is None
    assert resolve_business({"code": "999999", "name": "LG전자"}, config) is None
    assert resolve_business({"code": "066570", "name": "LG전자", "asset_type": "etf"}, config) is None


def test_official_disclosure_discovery_rejects_other_hosts_and_issuer_mismatch():
    config = Config(research_enabled=True, tavily_api_key=SecretStr("test-search"), _env_file=None)

    def handler(request):
        assert request.url.host == "api.tavily.com"
        payload = json.loads(request.content)
        assert payload["include_domains"] == ["dart.fss.or.kr", "kind.krx.co.kr"]
        assert "quantity" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://evil.test/company", "content": "합성기업 999999 반도체"},
                    {"url": "https://kind.krx.co.kr/report", "content": "합성기업 다른 코드 반도체"},
                    {
                        "url": "https://kind.krx.co.kr/disclosure",
                        "content": "합성기업 999999 반도체 장비 사업",
                    },
                ]
            },
        )

    context = resolve_business({"code": "999999", "name": "합성기업"}, config, httpx.MockTransport(handler))
    assert context["sources"] == ["https://kind.krx.co.kr/disclosure"]
    assert "evil" not in context["description"]


def test_discovery_disabled_never_calls_search_or_guesses_from_name():
    config = Config(research_enabled=False, tavily_api_key=SecretStr("test-search"), _env_file=None)

    def handler(request):
        raise AssertionError("Research is disabled")

    assert (
        resolve_business({"code": "999999", "name": "반도체 가전 수소"}, config, httpx.MockTransport(handler))
        is None
    )
