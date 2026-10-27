"""Explicit candidate assessments, not claims of 102 connected data series."""

from .dimension_providers import apply_provider
from .etf_expansion import apply_etf_expansion
from .kiwoom_catalog import apply_kiwoom
from .official_daily import apply_official_daily
from .dimension_twse import apply_twse
from .dimension_worldbank import apply_worldbank
from .dimension_imf import apply_imf
from .dimension_pending import apply_pending
from .dimension_krx import apply_krx
from .dimension_steel import apply_steel

GROUPS = {
    "sector": (
        "섹터·테마",
        "반도체|반도체 장비·소재|방산·항공우주|필수소비재·생필품|재생에너지|태양광|풍력|2차전지|배터리 소재|전기차|로봇·자동화|AI·소프트웨어|클라우드·데이터센터|전력기기·전력망|원전|바이오·헬스케어|은행|보험|증권|자동차|조선|해운|철강|화학|정유|건설|통신|유틸리티|유통|음식료|게임·콘텐츠|인터넷|리츠|반도체 소부장 기업|반도체 전공정 기업|반도체 패키징·후공정 기업|가전·백색가전|전자부품·IT하드웨어|태양광·ESS|수소·연료전지|디스플레이·OLED|공조·냉난방·냉각|비철금속·제련|화장품·생활용품",
    ),
    "commodity": (
        "원자재",
        "금|은|백금|팔라듐|구리|아연|알루미늄|니켈|납|주석|철광석 IMF · 월평균|열연강판|리튬|코발트|흑연|희토류·NdPr|WTI|Brent|Dubai|천연가스 Henry Hub|천연가스 TTF|LNG JKM|석탄|우라늄|옥수수|소맥|대두|대두유|팜유 · 2026 이후 월평균|설탕|커피 Arabica · 월평균|면화|고무 RSS3 · 월평균|목재|운임 BDI|컨테이너 운임 SCFI|철강 생산자물가지수 · 미국 · 월간",
    ),
    "market": (
        "국가·시장",
        "KOSPI|KOSPI 200|KOSDAQ|KOSDAQ 150|Nasdaq Composite|Nasdaq-100|S&P 500|Dow Jones Industrial Average|Russell 2000|S&P/ASX 200 (호주)|Nikkei 225|TOPIX|CSI 300|Hang Seng|TAIEX|STOXX Europe 600|DAX|FTSE 100|NIFTY 50|MSCI World|MSCI Emerging Markets",
    ),
    "control": (
        "환율·금리·위험",
        "USD/KRW|USD/JPY|EUR/USD|달러 지수 DXY|한국 국채 3년 금리|한국 국채 10년 금리|미국 국채 2년 금리|미국 국채 10년 금리|미국 장단기 금리차|VIX|VKOSPI|신용 스프레드",
    ),
}

# Only public-domain series are enabled. Publicly visible copyrighted series are
# documented but not silently treated as licensed collection feeds.
FRED = {
    "WTI": ("DCOILWTICO", "USD/배럴", "EIA · WTI Cushing 현물", True),
    "Brent": ("DCOILBRENTEU", "USD/배럴", "EIA · Europe Brent 현물", True),
    "천연가스 Henry Hub": ("DHHNGSP", "USD/MMBtu", "EIA · Henry Hub 현물", True),
    "USD/KRW": ("DEXKOUS", "KRW/USD", "Federal Reserve · 뉴욕 정오 매입환율", True),
    "USD/JPY": ("DEXJPUS", "JPY/USD", "Federal Reserve · 뉴욕 정오 매입환율", True),
    "EUR/USD": ("DEXUSEU", "USD/EUR", "Federal Reserve · 뉴욕 정오 매입환율", True),
    "미국 국채 2년 금리": ("DGS2", "%", "Federal Reserve · 2년 constant maturity yield", True),
    "미국 국채 10년 금리": ("DGS10", "%", "Federal Reserve · 10년 constant maturity yield", True),
    "미국 장단기 금리차": ("T10Y2Y", "%p", "FRED · 미국 국채 10년−2년 금리차", True),
    "Nasdaq Composite": ("NASDAQCOM", "지수", "Nasdaq · Composite 종가", False),
    "Nasdaq-100": ("NASDAQ100", "지수", "Nasdaq · Nasdaq-100 종가", False),
    "S&P 500": ("SP500", "지수", "S&P Dow Jones Indices · 가격지수", False),
    "Dow Jones Industrial Average": ("DJIA", "지수", "S&P Dow Jones Indices · DJIA", False),
    "VIX": ("VIXCLS", "지수", "Cboe · VIX 종가", False),
}

KRX_URL = "https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd"


def build_catalog():
    items = []
    for family, (group_name, labels) in GROUPS.items():
        for index, name in enumerate(labels.split("|"), 1):
            row = {
                "id": f"{family}-{index:02d}",
                "name": name,
                "family": family,
                "group_name": group_name,
                "assessment": "definition_pending",
                "daily_feasibility": "계열 정의·일별 제공 범위 확인 필요",
                "frequency": "미확정",
                "series_code": None,
                "unit": "미확정",
                "can_collect": False,
                "assessment_date": "2026-10-20",
                "reason": "정확한 지수·상품 코드와 일별 이력·이용권을 확정해야 합니다.",
                "source_name": "공급자 후보 미확정",
                "source_url": None,
                "access_method": "미연결",
                "license": "이용 조건 미확인",
                "calendar_note": "원천 관측일과 한국 거래일의 일치 여부는 수집 후 검사합니다.",
            }
            if family == "sector" or name in {"KOSPI", "KOSPI 200", "KOSDAQ", "KOSDAQ 150", "VKOSPI"}:
                row.update(
                    source_name="KRX 지수 API 후보",
                    source_url=KRX_URL,
                    reason="KRX 일별 지수 서비스는 존재합니다. 이 주제에 대응하는 정확한 지수 코드·신청 권한·역사 범위는 미확정입니다.",
                )
            elif family == "market":
                row.update(
                    source_name="해당 거래소·지수 제공자 / 계약형 데이터 공급자",
                    source_url="https://developers.lseg.com/en/api-catalog",
                    reason="공식 지수와 제공자·PR/TR·통화 및 수집 이용권 확정이 필요합니다. ETF로 임의 대체하지 않습니다.",
                )
            elif name in {"구리", "아연", "알루미늄", "니켈", "납", "주석", "코발트"}:
                row.update(
                    source_name="LME 공식 가격 후보",
                    source_url="https://www.lme.com/Market-data/Reports-and-data/LME-Official-Prices",
                    daily_feasibility="일별 공식 가격 존재 · 상품·이용권 확정 필요",
                    frequency="일별 후보",
                    reason="현물/3개월·규격·단위·라이선스와 실제 수집 경로를 확정해야 합니다.",
                )
            elif family == "commodity":
                row.update(
                    source_name="거래소·가격평가기관 후보",
                    source_url="https://www.cmegroup.com/market-data.html",
                )
                if name in {"흑연", "희토류·NdPr", "리튬", "우라늄"}:
                    row.update(
                        daily_feasibility="일별 표준계열 미확인 · 저빈도 가능",
                        reason="품질·지역·가격평가기관별 빈도를 확인해야 합니다. 주간/월간 자료를 일별로 채우지 않습니다.",
                    )
                if name in {"운임 BDI", "컨테이너 운임 SCFI"}:
                    row.update(
                        reason="운송비 대체 요인입니다. 일별/주간 원빈도·지수 정의·이용권을 확인해야 합니다."
                    )
            elif name in {"한국 국채 3년 금리", "한국 국채 10년 금리"}:
                row.update(
                    source_name="한국은행 ECOS 후보",
                    source_url="https://ecos.bok.or.kr/api/",
                    reason="통계표·항목 코드·일별 주기와 인증키를 연결해야 합니다.",
                )
            if name in FRED:
                code, unit, source, public = FRED[name]
                row.update(
                    series_code=code,
                    unit=unit,
                    frequency="일별",
                    source_name=source,
                    source_url=f"https://fred.stlouisfed.org/series/{code}",
                    access_method="FRED 공식 CSV (API 키 불필요)",
                    license="Public Domain · 출처 표기" if public else "저작권 계열 · 이용권 확인 필요",
                    assessment="collectable" if public else "license_required",
                    daily_feasibility="일별 수집 경로 연결" if public else "일별 계열 존재 · 이용권 대기",
                    can_collect=public,
                    reason="수집 버튼으로 선택 기간을 실제 확인합니다. 일별 관측이라도 공개 지연·해외 휴장으로 한국 거래일에 값이 없을 수 있습니다."
                    if public
                    else "공개 열람 가능 여부와 수집·가공 이용권은 다릅니다. 권한이 확인되기 전에는 자동 수집하지 않습니다.",
                )
            apply_provider(row)
            apply_kiwoom(row)
            apply_etf_expansion(row)
            apply_official_daily(row)
            apply_twse(row)
            apply_worldbank(row)
            apply_imf(row)
            apply_pending(row)
            apply_krx(row)
            apply_steel(row)
            if row.get("provider") in {"twse_taiex", "ecos", "eia_daily"}:
                row["implementation_status"] = "verified"
                row["implementation_label"] = "구현 확인"
                row["implementation_checked_on"] = "2026-10-27" if row["provider"] == "twse_taiex" else "2026-10-26"
                row["implementation_note"] = "수집기·실응답 검증 완료. 본인 계정의 실제 수집 성공·최신성은 별도 상태입니다."
            items.append(row)
    return items


DIMENSIONS = build_catalog()
BY_ID = {item["id"]: item for item in DIMENSIONS}
