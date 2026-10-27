from datetime import date

from nastack_trading.news_scope import relevance, six_month_start


def test_disclosures_require_exact_issuer_not_substring_or_referenced_code():
    for code, name, foreign in [
        ("005380", "현대차", "현대차증권"),
        ("005930", "삼성전자", "KIWOOM 삼성전자선물단일종목레버리지"),
        ("086520", "에코프로", "에코프로비엠"),
    ]:
        stock = {"code": code, "name": name}
        article = {
            "title": f"[유]{foreign} 공시 {name} {code}",
            "issuer": "[유]" + foreign,
            "content_type": "disclosure",
        }
        assert relevance(article, stock) is None
        assert relevance({**article, "issuer": None}, stock) is None
        own = {**article, "issuer": "[유]" + name}
        assert relevance(own, stock)["verified"] is False
        assert relevance({**own, "instrument_code": "999999"}, stock) is None
        assert relevance({**article, "instrument_code": code}, stock)["verified"] is True
    assert (
        relevance(
            {"title": "공시", "issuer": "[유]현대자동차", "content_type": "disclosure"},
            {"code": "005380", "name": "현대차"},
        )
        is not None
    )


def test_kind_same_title_different_receipts_are_not_dropped():
    from test_storage_news import clock

    from nastack_trading.news import parse_feed

    raw = b"<rss><channel><item><title>same filing</title><link>https://kind.krx.co.kr/common/disclsviewer.do?acptNo=1</link><pubDate>Tue, 20 Oct 2026 17:00:00 GMT</pubDate></item><item><title>same filing</title><link>https://kind.krx.co.kr/common/disclsviewer.do?acptNo=2</link><pubDate>Tue, 20 Oct 2026 17:00:00 GMT</pubDate></item></channel></rss>"
    items, _ = parse_feed(raw, date(2026, 10, 21), clock(), "https://kind.krx.co.kr/feed")
    assert [i["receipt_no"] for i in items] == ["1", "2"]


def test_calendar_six_months_not_180_days():
    assert six_month_start(date(2026, 10, 21)) == date(2026, 4, 21)
    assert six_month_start(date(2026, 8, 31)) == date(2026, 2, 28)
    assert six_month_start(date(2024, 8, 31)) == date(2024, 2, 29)


def test_issuer_and_industry_candidates_do_not_imply_impact():
    stock = {
        "code": "066570",
        "name": "LG전자",
        "trading_factor_profile": {
            "usable_as_topic_hint": True,
            "proposed_dimensions": [{"name": "가전·백색가전"}],
        },
    }
    assert relevance({"title": "LG전자 실적 공시"}, stock)["kind"] == "direct"
    assert relevance({"title": "Appliance market expands"}, stock)["kind"] == "industry_candidate"
    assert relevance({"title": "Appliance market expands", "content_type": "disclosure"}, stock) is None
    assert relevance({"title": "Copper price rises"}, stock) is None
    assert relevance({"title": "Appliance market expands"}, {**stock, "trading_factor_profile": {}}) is None
    assert (
        relevance({"title": "현대자동차 실적 공시"}, {"code": "005380", "name": "현대차"})["kind"] == "direct"
    )


def test_display_end_market_candidates_not_forced_impact():
    stock = {
        "code": "034220",
        "name": "LG디스플레이",
        "trading_factor_profile": {
            "usable_as_topic_hint": True,
            "proposed_dimensions": [{"name": "디스플레이·OLED"}],
        },
    }
    for title in [
        "OLED TV demand increases",
        "Smartphone shipments decline",
        "Laptop inventory rises",
        "차량용 디스플레이 채택 증가",
    ]:
        result = relevance({"title": title}, stock)
        assert result["kind"] == "industry_candidate" and not result["verified"]
    assert relevance({"title": "Server CPU shipments increase"}, stock) is None
    assert relevance({"title": "Smartphone shipments decline", "content_type": "disclosure"}, stock) is None
