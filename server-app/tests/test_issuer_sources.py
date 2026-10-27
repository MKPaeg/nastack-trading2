import httpx
import pytest

from nastack_trading.issuer_sources import business_text, issuer_listing, parse_listing


def listing(code="368600", name="아이씨에이치"):
    headers = ["회사명", "시장구분", "종목코드", "업종", "주요제품", "상장일", "결산월", "대표자명", "홈페이지", "지역"]
    row = [name, "코스닥", code, "전자부품 제조업", "스마트기기용 필름형 안테나", "2022-07-29", "12월", "대표자", "https://www.ch-ich.com", "경기도"]
    return "<table><tr>" + "".join(f"<th>{x}</th>" for x in headers) + "</tr><tr>" + "".join(f"<td>{x}</td>" for x in row) + "</tr></table>"


def test_official_listing_binds_name_code_products_and_homepage():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "kind.krx.co.kr" and request.method == "GET"
        return httpx.Response(200, content=listing().encode("euc-kr"))

    transport = httpx.MockTransport(handler)
    result = issuer_listing({"code": "368600", "name": "아이씨에이치"}, transport)
    assert result["products"] == "스마트기기용 필름형 안테나"
    assert result["homepage"] == "https://www.ch-ich.com"
    assert "368600" in result["snippet"]
    assert issuer_listing({"code": "368600", "name": "다른기업"}, transport) is None
    assert issuer_listing({"code": "000000", "name": "아이씨에이치"}, transport) is None
    assert len(calls) == 3


def test_schema_change_and_duplicate_identity_fail_closed():
    with pytest.raises(ValueError):
        parse_listing("<table><tr><th>회사명</th></tr></table>")
    body = listing()
    row = body[body.index("<tr><td>"):body.index("</tr></table>") + 5]
    with pytest.raises(ValueError):
        parse_listing(body.replace("</table>", row + "</table>"))


def test_navigation_is_not_business_content():
    assert not business_text("1. 사업의 개요 2. 주요 제품 및 서비스 3. 원재료 및 생산설비 문서 목차 정정문서를 확인하세요")
    assert not business_text("주주총회 개최 장소 및 기준일 안내")
    assert business_text("당사는 의료 영상 판독 솔루션을 개발하고 공급합니다")


def test_issuer_products_and_verified_homepage_are_used_when_disclosure_is_navigation(setup):
    import json

    from pydantic import SecretStr

    from nastack_trading.business_research import search_and_summarize

    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=listing().encode("euc-kr"))
        calls.append(request)
        payload = json.loads(request.content)
        if request.url.host == "api.tavily.com":
            assert "ch-ich.com" in payload["include_domains"]
            return httpx.Response(200, json={"results": [
                {"title": "아이씨에이치 공시", "content": "1. 사업의 개요 2. 주요 제품 및 서비스 문서 목차", "url": "https://kind.krx.co.kr/view"},
                {"title": "ICH products", "content": "We manufacture film antennas for mobile devices.", "url": "https://ch-ich.com/products"}]})
        evidence = json.loads(payload["messages"][1]["content"])["evidence"]
        assert evidence[0]["source_kind"] == "official_issuer_listing"
        assert len(evidence) == 2 and "manufacture" in evidence[1]["snippet"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"claims": [
            {"text": "스마트기기용 필름형 안테나", "evidence_id": "E1", "quote": "스마트기기용 필름형 안테나"}],
            "topics": ["전자부품"], "uncertainties": ["매출비중 미확인"]})}}]})

    config = setup[0].model_copy(update={"vllm_base_url": "http://model.test/v1", "tavily_api_key": SecretStr("synthetic")})
    context = search_and_summarize({"code": "368600", "name": "아이씨에이치"}, config,
        {"provider": "vllm", "model": "synthetic"}, {"text": "근거 요약", "version": 1, "sha256": "test"},
        set(), httpx.MockTransport(handler))
    assert context["topics"] == ["전자부품"] and len(calls) == 2


@pytest.mark.parametrize("same_tenant", [True, False])
def test_broker_identity_cache_is_private_and_disabled_connection_does_not_fallback(setup, monkeypatch, same_tenant):
    from sqlalchemy import select

    from nastack_trading.issuer_sources import broker_identity
    from nastack_trading.models import BrokerConnection, Tenant, User
    from nastack_trading.trade_gateway import TradeGateway

    calls = []

    def request(self, db, user, broker, path, body):
        assert broker == "namuh" and path == "/krstock/quote/v1/currentPrice"
        calls.append(user.id)
        return {"Output_0": {"iem_cd": "368600", "iem_nm": "시험기업", "bstp_kor_isnm": "전자", "bstp_cls_code": "CODE"}}, "live"

    monkeypatch.setattr(TradeGateway, "request", request)
    instrument = {"code": "368600", "name": "시험기업"}
    with setup[1].begin() as db:
        first = db.scalar(select(User))
        tenant = first.tenant_id
        if not same_tenant:
            other_tenant = Tenant(name="issuer-cache-other", timezone="Asia/Seoul")
            db.add(other_tenant)
            db.flush()
            tenant = other_tenant.id
        second = User(tenant_id=tenant, email="issuer-other@example.com", active=True, role="trader", code_hash="unused")
        db.add(second)
        db.flush()
        first_conn = BrokerConnection(owner_id=first.id, tenant_id=first.tenant_id, enabled=True, environment="live", version=1)
        second_conn = BrokerConnection(owner_id=second.id, tenant_id=second.tenant_id, enabled=True, environment="live", version=1)
        db.add_all([first_conn, second_conn])
        db.flush()
        config = setup[0].model_copy(update={"namuh_owner_email": first.email})
        assert broker_identity(db, first, config, instrument)["status"] == "confirmed"
        assert broker_identity(db, first, config, instrument)["reused"] is True
        second_config = config.model_copy(update={"namuh_owner_email": second.email})
        assert broker_identity(db, second, second_config, instrument)["status"] == "confirmed"
        assert calls == [first.id, second.id]
        second_conn.enabled = False
        db.flush()
        assert broker_identity(db, second, second_config, instrument)["status"] == "not_connected"
        assert calls == [first.id, second.id]
        first.role = "viewer"
        db.flush()
        assert broker_identity(db, first, config, instrument)["status"] == "not_authorized"
        assert calls == [first.id, second.id]
