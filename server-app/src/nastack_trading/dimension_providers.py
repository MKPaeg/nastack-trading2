"""Explicit proxy definitions. ETF prices are never labelled as original indices."""

# Code/name pairs checked against the NH PLUG official instrument master.
ETF_PROXIES = {
    "가전·백색가전": ("266390", "KODEX 경기소비재"),
    "전자부품·IT하드웨어": ("266370", "KODEX IT"),
    "태양광·ESS": ("457990", "PLUS 태양광&ESS"),
    "수소·연료전지": ("367770", "RISE 수소경제테마"),
    "화장품·생활용품": ("228790", "TIGER 화장품"),
    "반도체 소부장 기업": ("455850", "SOL AI반도체소부장"),
    "반도체 전공정 기업": ("475300", "SOL 반도체전공정"),
    "반도체 패키징·후공정 기업": ("475310", "SOL 반도체후공정"),
    "반도체": ("091160", "KODEX 반도체"),
    "반도체 장비·소재": ("471990", "KODEX AI반도체핵심장비"),
    "방산·항공우주": ("463250", "TIGER K방산&우주"),
    "필수소비재·생필품": ("266410", "KODEX 필수소비재"),
    "재생에너지": ("385510", "KODEX 신재생에너지액티브"),
    "태양광": ("413220", "SOL 차이나태양광CSI(합성)"),
    "2차전지": ("305720", "KODEX 2차전지산업"),
    "배터리 소재": ("461950", "KODEX 2차전지핵심소재10"),
    "로봇·자동화": ("445290", "KODEX 로봇액티브"),
    "클라우드·데이터센터": ("371450", "TIGER 글로벌클라우드컴퓨팅INDXX"),
    "전력기기·전력망": ("487240", "KODEX AI전력핵심설비"),
    "원전": ("433500", "ACE 원자력TOP10"),
    "바이오·헬스케어": ("266420", "KODEX 헬스케어"),
    "은행": ("091170", "KODEX 은행"),
    "보험": ("140700", "KODEX 보험"),
    "증권": ("102970", "KODEX 증권"),
    "자동차": ("091180", "KODEX 자동차"),
    "조선": ("445150", "KODEX 친환경조선해운액티브"),
    "철강": ("117680", "KODEX 철강"),
    "건설": ("117700", "KODEX 건설"),
    "유틸리티": ("463640", "KODEX 미국S&P500유틸리티"),
    "게임·콘텐츠": ("300950", "KODEX 게임산업"),
    "인터넷": ("365000", "TIGER 인터넷TOP10"),
    "리츠": ("476800", "KODEX 한국부동산리츠인프라"),
    "금": ("132030", "KODEX 골드선물(H)"),
    "은": ("144600", "KODEX 은선물(H)"),
    "팔라듐": ("334690", "RISE 팔라듐선물(H)"),
    "구리": ("138910", "KODEX 구리선물(H)"),
    "대두": ("138920", "KODEX 콩선물(H)"),
    "KOSPI": ("226490", "KODEX 코스피"),
    "KOSPI 200": ("069500", "KODEX 200"),
    "KOSDAQ 150": ("229200", "KODEX 코스닥150"),
    "Nasdaq-100": ("379810", "KODEX 미국나스닥100"),
    "S&P 500": ("379800", "KODEX 미국S&P500"),
    "Dow Jones Industrial Average": ("245340", "TIGER 미국다우존스30"),
    "Russell 2000": ("280930", "KODEX 미국러셀2000(H)"),
    "Nikkei 225": ("241180", "TIGER 일본니케이225"),
    "TOPIX": ("195920", "TIGER 일본TOPIX(합성 H)"),
    "CSI 300": ("283580", "KODEX 차이나CSI300"),
    "NIFTY 50": ("453810", "KODEX 인도Nifty50"),
    "MSCI World": ("251350", "KODEX MSCI선진국"),
    "MSCI Emerging Markets": ("195980", "PLUS 신흥국MSCI(합성 H)"),
    "달러 지수 DXY": ("261240", "KODEX 미국달러선물"),
}


def apply_provider(row):
    """Attach a supported series or a concrete procurement route, not fake data."""
    name = row["name"]
    row["provider"] = "fred" if row["series_code"] else None
    row["representation"] = "original_series"
    if name in ETF_PROXIES:
        code, product = ETF_PROXIES[name]
        row.update(
            provider="namuh_etf",
            representation="etf_proxy",
            series_code=code,
            instrument_name=product,
            series_label=f"{name} · ETF 대체: {product}",
            source_name=f"NH PLUG · {product} ({code}) · ETF 대체 지표",
            source_url="https://www.nhplug.com/",
            unit="KRW/ETF 1좌",
            frequency="한국 거래일",
            assessment="collectable",
            can_collect=True,
            access_method="NH PLUG currentDaily · 기존 소유자 인증 · 최근 최대 250거래일",
            daily_feasibility="ETF 일별 종가 수집 연결 · 원지수 아님",
            license="기존 NH API 소유자 개인 분석 범위 · 재배포/자동매매 이용권 별도 확인",
            reason=f"{product}의 원화 종가를 대체 지표로 수집합니다. 원지수·현물 가격이 아닙니다. "
            "국가·구성종목·액티브 운용·환율·환헤지·선물 롤오버·보수·분배금 영향을 포함할 수 있습니다. "
            "수정주가/총수익 검증 전이며, 최근 250거래일 밖 자료를 보간하지 않습니다.",
        )
        if name in {"반도체 소부장 기업", "반도체 전공정 기업", "반도체 패키징·후공정 기업"}:
            row.update(
                assessment_date="2026-10-21",
                issuer_url="https://www.soletf.co.kr/ko/strategy/pension",
                proxy_definition="반도체 공급망 관련 기업 바스켓 ETF 주가. 개별 웨이퍼·가스·약품·포토레지스트·패키징 재료 가격이나 물량이 아님. 소재별 편입·노출 비중은 별도 검증 필요.",
            )
            row["reason"] += (
                " 웨이퍼·특수가스·세정/식각·포토·CMP·패키징 재료 각각의 가격으로 분해하거나 동일 ETF를 여러 원재료 가격 차원으로 복제하지 않습니다."
            )
        if name in {"가전·백색가전", "전자부품·IT하드웨어", "태양광·ESS", "수소·연료전지", "화장품·생활용품"}:
            definitions = {
                "가전·백색가전": "경기소비재 전체 ETF인 광역 대체 지표. 가전 전용 지수가 아니며 자동차·유통 등 타 업종을 포함할 수 있음. LG전자 등 분석 대상 종목 편입에 따른 자기상관 주의.",
                "전자부품·IT하드웨어": "KRX IT 추종 광역 ETF. 반도체·전기전자·IT하드웨어를 포괄하며 MLCC 전용 지수가 아님. 분석 대상 종목 편입에 따른 자기상관 주의.",
                "태양광·ESS": "태양광 및 ESS 기업 혼합 ETF. ESS 단독 지수나 전력·배터리 원재료 가격이 아님.",
                "수소·연료전지": "수소 생산·저장·유통·발전·수소차 밸류체인 ETF. 연료전지 단독 지수 또는 수소 현물가격이 아님.",
                "화장품·생활용품": "화장품 기업 ETF 대체 지표. 생활용품·음료 전체와 동일하지 않으며 분석 대상 종목 편입에 따른 자기상관 주의.",
            }
            row.update(assessment_date="2026-10-21", proxy_definition=definitions[name])
            row["reason"] += " " + definitions[name]
        return
    if row["can_collect"]:
        return
    if row["family"] == "sector" or name in {"KOSDAQ", "VKOSPI"}:
        provider, url = "KRX OPEN API", "https://openapi.krx.co.kr/"
    elif name in {"한국 국채 3년 금리", "한국 국채 10년 금리"}:
        provider, url = "한국은행 ECOS", "https://ecos.bok.or.kr/api/"
    elif name in {"운임 BDI"}:
        provider, url = "Baltic Exchange", "https://www.balticexchange.com/en/data-services.html"
    elif name == "컨테이너 운임 SCFI":
        provider, url = "Shanghai Shipping Exchange", "https://en.sse.net.cn/indices/scfinew.jsp"
    elif name in {"Dubai", "LNG JKM"}:
        provider, url = (
            "S&P Global Commodity Insights / Platts",
            "https://www.spglobal.com/commodityinsights/ja/data-and-distribution",
        )
    elif name in {"천연가스 TTF", "설탕", "커피", "면화"}:
        provider, url = "ICE Data Services", "https://developer.ice.com/"
    elif name == "석탄":
        provider, url = "ICE Data Services · 석탄 선물", "https://www.ice.com/energy/coal"
    elif name == "우라늄":
        provider, url = "UxC · 우라늄 가격", "https://www.uxc.com/p/product/periodical/uxw"
    elif name in {"아연", "알루미늄", "니켈", "납", "주석", "코발트", "열연강판"}:
        provider, url = "LME / 공인 데이터 배포사", "https://www.lme.com/Market-data/Accessing-market-data"
    elif name in {"리튬", "흑연", "희토류·NdPr", "철광석", "고무", "팜유"}:
        provider, url = "Fastmarkets", "https://www.fastmarkets.com/platform/api/"
    elif name in {"백금", "옥수수", "소맥", "대두유", "목재"}:
        provider, url = "CME Group Data", "https://www.cmegroup.com/market-data.html"
    elif row["series_code"]:
        return  # Existing FRED licensed series retain their verified owner and code.
    elif row["family"] == "market" or name == "신용 스프레드":
        provider, url = (
            "LSEG Data Platform · 계약형 일별 데이터",
            "https://developers.lseg.com/en/api-catalog/refinitiv-data-platform/refinitiv-data-platform-apis",
        )
    else:
        # Do not claim unverified coverage for the remaining benchmarks.
        return
    row.update(
        source_name=provider,
        source_url=url,
        provider=provider,
        access_method="공급자 선정 · 인증/상품 코드/이용권 확인 필요 · 수집기 미연결",
        reason=f"선정 공급자: {provider}. 해당 계열의 상품·지역·규격·빈도·과거 범위와 API 권한을 확인해야 합니다. "
        "계약/인증 승인 전 수집하거나 ETF로 임의 대체하지 않습니다.",
    )
