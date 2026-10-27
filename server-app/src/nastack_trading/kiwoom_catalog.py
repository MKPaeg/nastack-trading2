"""Mappings verified with Kiwoom ka10101 on 2026-10-20; no ETF replacement."""

KIWOOM_SERIES = {
    "AI·소프트웨어": ("029", "IT 서비스", "0", True),
    "화학": ("008", "화학", "0", False),
    "통신": ("020", "통신", "0", False),
    "유통": ("016", "유통", "0", False),
    "음식료": ("005", "음식료/담배", "0", True),
    "KOSDAQ": ("101", "종합(KOSDAQ)", "1", False),
}


def apply_kiwoom(row):
    if row["name"] not in KIWOOM_SERIES:
        return
    code, name, market, proxy = KIWOOM_SERIES[row["name"]]
    if row["can_collect"]:
        raise ValueError("Never silently replace an existing connected series")
    row.update(
        provider="kiwoom_index",
        series_code=code,
        instrument_name=name,
        market_code=market,
        representation="industry_proxy" if proxy else "original_series",
        series_label=f"{row['name']} · 키움 {'업종 대체' if proxy else '지수'}: {name}",
        source_name="키움 REST API · KOSDAQ 지수"
        if code == "101"
        else f"키움 REST API · KOSPI {name} 업종지수",
        source_url="https://openapi.kiwoom.com/m/guide/apiguide?apiId=ka20006",
        unit="지수 포인트",
        frequency="한국 거래일",
        can_collect=True,
        assessment="collectable",
        daily_feasibility="업종 코드 목록·일봉 API 연결",
        access_method="키움 REST ka10101 / ka20006 · 기존 소유자 인증 · 연속조회 최대 10페이지",
        license="기존 키움 API 소유자 개인 분석 · 재배포/자동매매 이용권 별도 확인",
        reason=(
            f"실제 계열은 {name}({code})입니다. "
            + (
                "분석 주제 전체와 동일하지 않은 업종 대체 지표입니다. "
                if proxy
                else "ETF 가격이 아닌 지수 원계열입니다. "
            )
            + "공식 업종 목록의 코드·명칭을 수집 시 대조합니다. 원응답의 부호는 등락 표시로 처리하고 절댓값을 100으로 나눠 지수 포인트로 복원합니다. "
            "기존 ETF 자료와 섞지 않으며 발표 당시 이용 가능 여부는 미검증입니다."
        ),
    )
