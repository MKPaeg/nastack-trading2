"""Explicit ETF proxies for previously uncollected dimensions; no ETN/ETC."""

# Country/price/basket differences must remain visible; these are not original prices.
LOCAL_ETFS = {
    "전기차": ("371460", "TIGER 차이나전기차SOLACTIVE", "중국 전기차·배터리 기업"),
    "리튬": ("394670", "TIGER 글로벌리튬&2차전지SOLACTIVE(합성)", "리튬·배터리 기업 주가, 리튬 가격 아님"),
    "희토류·NdPr": (
        "415920",
        "PLUS 글로벌희토류&전략자원생산기업",
        "희토류·전략자원 기업 주가, NdPr 가격 아님",
    ),
    "DAX": ("411860", "KIWOOM 독일DAX", "독일 DAX 추종 ETF"),
    "한국 국채 3년 금리": (
        "114260",
        "KODEX 국고채3년",
        "국고채 ETF 가격, 금리 수치가 아니며 금리와 반대로 움직일 수 있음",
    ),
    "한국 국채 10년 금리": (
        "471230",
        "KODEX 국고채10년액티브",
        "국고채 ETF 가격, 금리 수치가 아니며 금리와 반대로 움직일 수 있음",
    ),
}

# Symbol, Kiwoom exchange, name, definition, issuer evidence.
US_ETFS = {
    "풍력": (
        "FAN",
        "NY",
        "First Trust Global Wind Energy ETF",
        "글로벌 풍력 기업 주가",
        "https://www.ftportfolios.com/retail/etf/etfsummary.aspx?ticker=FAN",
    ),
    "해운": (
        "BOAT",
        "NY",
        "SonicShares Global Shipping ETF",
        "글로벌 해운기업 주가, 운임 아님",
        "https://sonicshares.com/boat/",
    ),
    "정유": (
        "CRAK",
        "NY",
        "VanEck Oil Refiners ETF",
        "글로벌 정유기업 주가, 원유가격 아님",
        "https://www.vaneck.com/us/en/investments/oil-refiners-etf-crak/",
    ),
    "백금": (
        "PPLT",
        "NY",
        "abrdn Physical Platinum Shares ETF",
        "백금 현물 보유 ETF",
        "https://www.aberdeeninvestments.com/en-us/investor/funds/view-all-funds/-us0032601066",
    ),
    "니켈": (
        "NIKL",
        "ND",
        "Sprott Nickel Miners ETF",
        "니켈 채굴기업 주가, 니켈가격 아님",
        "https://sprottetfs.com/nikl-sprott-nickel-miners-etf/",
    ),
    "석탄": (
        "COAL",
        "NY",
        "Range Global Coal Index ETF",
        "석탄 관련 기업 주가, 석탄가격 아님",
        "https://www.rangeetfs.com/coal",
    ),
    "우라늄": (
        "URA",
        "NY",
        "Global X Uranium ETF",
        "우라늄·원전 관련 기업 중심, 우라늄 현물가격 아님",
        "https://www.globalxetfs.com/funds/ura",
    ),
    "옥수수": (
        "CORN",
        "NY",
        "Teucrium Corn Fund",
        "옥수수 선물 ETF, 롤오버 포함",
        "https://teucrium.com/CORN",
    ),
    "소맥": ("WEAT", "NY", "Teucrium Wheat Fund", "소맥 선물 ETF, 롤오버 포함", "https://teucrium.com/WEAT"),
    "설탕": ("CANE", "NY", "Teucrium Sugar Fund", "설탕 선물 ETF, 롤오버 포함", "https://teucrium.com/CANE"),
    "목재": (
        "WOOD",
        "ND",
        "iShares Global Timber & Forestry ETF",
        "목재·임업기업 주가, 목재 선물가격 아님",
        "https://www.ishares.com/us/literature/fact-sheet/wood-ishares-global-timber-forestry-etf-fund-fact-sheet-en-us.pdf",
    ),
    "운임 BDI": (
        "BDRY",
        "NY",
        "Breakwave Dry Bulk Shipping ETF",
        "건화물 운임 선물 ETF, BDI 원지수 아님",
        "https://amplifyetfs.com/bdry/",
    ),
    "Nasdaq Composite": (
        "ONEQ",
        "ND",
        "Fidelity Nasdaq Composite Index ETF",
        "Nasdaq Composite 추종 ETF",
        "https://digital.fidelity.com/prgw/digital/research/quote/dashboard/summary?symbol=ONEQ",
    ),
    "VIX": (
        "VIXY",
        "NY",
        "ProShares VIX Short-Term Futures ETF",
        "VIX 단기 선물 ETF, VIX 현물과 다르며 롤오버 포함",
        "https://www.proshares.com/our-etfs/strategic/vixy",
    ),
    "신용 스프레드": (
        "HYHG",
        "NY",
        "ProShares High Yield Interest Rate Hedged ETF",
        "금리위험 헤지 하이일드 채권 ETF 가격, 스프레드 수치 아님",
        "https://www.proshares.com/our-etfs/strategic/hyhg",
    ),
}

# Do not remove real ETFs merely because current adapters lack these exchanges.
OTHER_EXCHANGE_ETFS = {
    "S&P/ASX 200 (호주)": ("IOZ", "ASX", "https://www.blackrock.com/au/products/251852/ishares-core-s"),
    "Hang Seng": ("2800", "HKEX", "https://www.trahk.com.hk/en-hk/trahk-fund/"),
    "TAIEX": ("006204", "TWSE", "https://sitc.sinopac.com/SinopacEtfs/Etfs/Pcf/006204"),
    "STOXX Europe 600": (
        "EXSA",
        "XETRA",
        "https://www.ishares.com/de/privatanleger/de/produkte/251931/ishares-stoxx-europe-600-ucits-etf-de-fund",
    ),
    "FTSE 100": (
        "ISF",
        "LSE",
        "https://www.ishares.com/uk/individual/en/products/251795/ishares-ftse-100-ucits-etf-inc-fund",
    ),
}

NO_SUITABLE_ETF = {
    "아연",
    "알루미늄",
    "납",
    "주석",
    "철광석",
    "열연강판",
    "코발트",
    "흑연",
    "Dubai",
    "천연가스 TTF",
    "LNG JKM",
    "대두유",
    "팜유",
    "커피",
    "면화",
    "고무",
    "컨테이너 운임 SCFI",
    "VKOSPI",
}


def apply_etf_expansion(row):
    name = row["name"]
    if name in LOCAL_ETFS:
        code, product, definition = LOCAL_ETFS[name]
        row.update(
            provider="namuh_etf",
            instrument_name=product,
            series_code=code,
            source_url="https://www.nhplug.com/",
            unit="KRW/ETF 1좌",
            currency="KRW",
            access_method="NH PLUG currentDaily · 최근 최대 250거래일",
        )
    elif name in US_ETFS:
        code, exchange, product, definition, evidence = US_ETFS[name]
        row.update(
            provider="kiwoom_us_etf",
            instrument_name=product,
            series_code=code,
            exchange_code=exchange,
            issuer_url=evidence,
            source_url="https://openapi.kiwoom.com/",
            unit="USD/ETF 1좌",
            currency="USD",
            access_method="키움 미국 ETF 종목 확인·일봉 · 최대 10페이지",
        )
    elif name in OTHER_EXCHANGE_ETFS:
        code, exchange, evidence = OTHER_EXCHANGE_ETFS[name]
        row.update(
            etf_exists=True,
            source_url=evidence,
            issuer_url=evidence,
            reason=f"대응 ETF {code} ({exchange}) 확인. 현재 국내/미국 수집기는 해당 거래소를 지원하지 않아 연결 대기입니다. ETF가 없는 것으로 삭제하지 않습니다.",
        )
        return
    else:
        return
    row.update(
        etf_exists=True,
        representation="etf_proxy",
        proxy_definition=definition,
        source_name=f"{'키움 REST' if row['provider'] == 'kiwoom_us_etf' else 'NH PLUG'} · {product} ({code}) · ETF 대체 지표",
        series_label=f"{name} · ETF 대체: {product}",
        assessment="collectable",
        can_collect=True,
        frequency="미국 거래일" if row["provider"] == "kiwoom_us_etf" else "한국 거래일",
        daily_feasibility="ETF 대체 수집기 연결 · 실제 성공은 별도 확인",
        license="기존 증권 API 소유자 개인 분석 · 재배포/자동매매 이용권 별도 확인",
        reason=f"실제 수집값: {product}의 {row['currency']} 가격. {definition}. "
        "분석 주제의 원지수·현물·금리·스프레드 값과 동일하지 않습니다. 환율·분배금·보수·구성종목·롤오버 영향을 구분해야 합니다.",
    )
