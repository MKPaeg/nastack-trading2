"""Source-backed public business context. Never infer an issuer's business from its name.

The reviewed registry bootstraps current issuers. Other issuers may supply an
administrator-confirmed description, or use bounded official-disclosure search
when research is enabled. Missing evidence is a review state, not a market-only success.
"""

import hashlib
from urllib.parse import urlsplit

from .db import now
from .research import ResearchError, ResearchPipeline, public_url

REVIEWED_ON = "2026-10-21"
REVIEWED_DATES = {"494310": "2026-10-24"}
# code: public name, concise paraphrase, source, business-related candidate names.
REVIEWED = {
    "494310": (
        "KODEX 반도체레버리지",
        "삼성자산운용의 반도체 섹터 ETF. KRX 반도체 지수의 일간 수익률 양의 2배 연동을 목표로 하며 기간 누적 수익률의 2배를 보장하지 않는다. 제조기업이 아니라 파생상품 등을 사용하는 투자상품이다.",
        "https://m.samsungfund.com/sheet/20250610/2ETFO8_20250530.pdf",
        ["반도체"],
    ),
    "046120": (
        "오르비텍",
        "원자력발전소 방사선관리·검사 및 항공기 부품 제조 사업.",
        "https://kind.krx.co.kr/external/2024/03/20/000473/20240320002456/11011.htm",
        ["원전", "방산·항공우주"],
    ),
    "066570": (
        "LG전자",
        "생활가전, TV와 디스플레이 제품, 차량 부품 및 냉난방·공조 솔루션 사업.",
        "https://www.lg.com/content/dam/lge/global/sustainability/pdf/2024-2025_LGE_Sustainability_Report_English.pdf",
        ["가전·백색가전", "전자부품·IT하드웨어", "공조·냉난방·냉각", "디스플레이·OLED"],
    ),
    "005930": (
        "삼성전자",
        "반도체와 스마트폰, TV, 생활가전을 포함한 디바이스 사업.",
        "https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2025_4Q_Interim_Report.pdf",
        ["반도체", "가전·백색가전", "전자부품·IT하드웨어", "디스플레이·OLED"],
    ),
    "009150": (
        "삼성전기",
        "MLCC 등 전자부품, 패키지 기판, 카메라 모듈을 개발·생산.",
        "https://m.samsungsem.com/global/about-us/company/overview.do",
        ["전자부품·IT하드웨어", "반도체 패키징·후공정 기업"],
    ),
    "036930": (
        "주성엔지니어링",
        "반도체, 디스플레이 및 태양전지 제조 장비 사업.",
        "https://www.jusung.com/eng/document/introduce",
        ["반도체 장비·소재", "반도체 전공정 기업", "태양광", "디스플레이·OLED"],
    ),
    "475150": (
        "SK이터닉스",
        "태양광, 풍력, ESS와 연료전지 발전 사업 개발·운영.",
        "https://www.sketernix.com/company/overview.do",
        ["재생에너지", "태양광", "풍력", "태양광·ESS", "수소·연료전지"],
    ),
    "010130": (
        "고려아연",
        "아연·연 등 비철금속 제련 및 은·금·구리 등 금속 생산.",
        "https://www.koreazinc.co.kr/wp-content/uploads/2025/07/2025-KoreaZinc-Brochure_ENG.pdf",
        ["비철금속·제련", "아연", "납", "은", "금", "구리"],
    ),
    "440110": (
        "파두",
        "데이터센터용 기업용 SSD 컨트롤러를 설계하는 반도체 팹리스 기업.",
        "https://www.fadu.io/product/ssd-controllers/?lang=en",
        ["반도체", "클라우드·데이터센터"],
    ),
    "005380": (
        "현대차",
        "승용·상용 자동차와 전기차, 수소연료전지 차량 사업.",
        "https://www.hyundai.com/worldwide/en/company/innovation/electrification",
        ["자동차", "전기차", "수소·연료전지"],
    ),
    "001440": (
        "대한전선",
        "초고압 전력 케이블과 해저 케이블 등 전선 사업.",
        "https://www.taihan.com/en",
        ["전력기기·전력망", "구리"],
    ),
    "108490": (
        "로보티즈",
        "로봇 관절용 액추에이터와 로봇 부품·플랫폼 사업.",
        "https://en.robotis.com/model/page.php?co_id=actuator",
        ["로봇·자동화"],
    ),
    "000720": (
        "현대건설",
        "토목·건축·주택·플랜트·에너지 분야 건설 사업.",
        "https://newsroom.hdec.kr/downloadfile/2025_HDEC_Sustainability_Report_K.pdf",
        ["건설"],
    ),
    "329180": (
        "HD현대중공업",
        "조선, 해양 및 엔진기계 사업.",
        "https://www.hhi.co.kr/filedown/2023_KSOE_IR_KR.pdf",
        ["조선"],
    ),
    "042660": (
        "한화오션",
        "상선, 특수선 및 해양 설비 사업.",
        "https://www.hanwhaocean.com/pdf/Hanwha_Ocean_Brochure_KR_2025.pdf",
        ["조선", "방산·항공우주"],
    ),
    "047810": (
        "한국항공우주",
        "항공기 개발·제조 및 위성 등 항공우주 사업.",
        "https://www.koreaaero.com/KO/",
        ["방산·항공우주"],
    ),
    "272210": (
        "한화시스템",
        "방산 전자·항공우주 솔루션 및 ICT 서비스 사업.",
        "https://www.hanwhasystems.com/kr/business/itservice/2/view.do",
        ["방산·항공우주", "AI·소프트웨어"],
    ),
    "003670": (
        "포스코퓨처엠",
        "이차전지 양극재·음극재 및 산업기초소재 사업.",
        "https://www.poscofuturem.com/business/energy.do",
        ["배터리 소재", "2차전지", "리튬", "니켈"],
    ),
    "047050": (
        "포스코인터내셔널",
        "에너지 개발·발전 및 철강·식량·부품소재 국제무역 사업.",
        "https://www.poscointl.com/about",
        ["철강", "유틸리티", "천연가스 Henry Hub"],
    ),
    "022100": (
        "포스코DX",
        "산업용 AI·IT 기반 스마트팩토리, 산업용 로봇과 스마트물류 솔루션.",
        "https://www.poscodx.com/kor/about/poscodx",
        ["AI·소프트웨어", "로봇·자동화"],
    ),
    "247540": (
        "에코프로비엠",
        "이차전지용 하이니켈 양극소재 사업.",
        "https://www.ecoprobm.co.kr/_files/brochure_pdf/20240701/9872fe4980a95bda555779be056904ed.pdf",
        ["배터리 소재", "2차전지", "니켈", "리튬"],
    ),
    "086520": (
        "에코프로",
        "지주회사로서 이차전지 소재 및 환경 사업 계열사를 경영.",
        "https://www.ecopro.co.kr/sub010101",
        ["배터리 소재", "2차전지"],
    ),
    "011070": (
        "LG이노텍",
        "광학솔루션, 기판소재 및 전장부품 사업.",
        "https://www.lginnotek.com/",
        ["전자부품·IT하드웨어", "반도체 패키징·후공정 기업"],
    ),
    "034220": (
        "LG디스플레이",
        "OLED 등 디스플레이 패널과 관련 제품을 생산·판매.",
        "https://www.lgdisplay.com/eng/company/info/outline/vision",
        ["디스플레이·OLED", "전자부품·IT하드웨어"],
    ),
    "051900": (
        "LG생활건강",
        "화장품, 생활용품 및 음료 사업.",
        "https://www.lghnh.com/m/recruit/info/intro.jsp",
        ["화장품·생활용품", "필수소비재·생필품", "음식료"],
    ),
    "373220": (
        "LG에너지솔루션",
        "자동차전지, 소형전지 및 ESS 배터리 사업.",
        "https://www.lgensol.com/kr/company/info-outline",
        ["2차전지", "배터리 소재", "태양광·ESS"],
    ),
    "051910": (
        "LG화학",
        "석유화학, 첨단소재 및 생명과학 사업.",
        "https://www.lgchem.com/main/index?lang=ko_KR",
        ["화학", "배터리 소재", "바이오·헬스케어"],
    ),
}


def resolve_business(instrument, config, transport=None):
    """Public metadata only. No arbitrary URL fetching or name-only business guesses."""
    code = instrument["code"]
    entry = REVIEWED.get(code)
    if entry and instrument.get("asset_type") != "etf":
        name, description, url, topics = entry
        if instrument.get("name") != name:
            return None  # Avoid assigning facts to a different issuer sharing bad metadata.
        value = {
            "description": description,
            "sources": [url],
            "topics": topics,
            "source_kind": "reviewed_official",
            "reviewed_on": REVIEWED_DATES.get(instrument["code"], REVIEWED_ON),
        }
    elif instrument.get("business_description") and public_url(instrument.get("business_source_url")):
        value = {
            "description": instrument["business_description"][:4000],
            "sources": [instrument["business_source_url"]],
            "topics": [],
            "source_kind": "administrator_supplied",
        }
    elif config and config.research_enabled and config.tavily_api_key.get_secret_value():
        try:
            result = ResearchPipeline(config, transport).post(
                "https://api.tavily.com/search",
                config.tavily_api_key.get_secret_value(),
                {
                    "query": f'"{instrument["name"]}" "{code}" 사업의 내용 주요 사업',
                    "include_domains": ["dart.fss.or.kr", "kind.krx.co.kr"],
                    "search_depth": "basic",
                    "max_results": 3,
                    "include_raw_content": False,
                },
                10,
            )
            excerpts = []
            for item in result.get("results", [])[:3]:
                if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                    continue
                url = public_url(item.get("url"))
                host = (urlsplit(url).hostname or "") if url else ""
                text = str(item.get("content", ""))[:1500]
                if (
                    host not in {"dart.fss.or.kr", "kind.krx.co.kr"}
                    or instrument["name"] not in text
                    or code not in text + str(item.get("title", ""))
                    or not text.strip()
                ):
                    continue
                excerpts.append((url, text))
            if not excerpts:
                return None
            value = {
                "description": "\n".join(text for _, text in excerpts)[:4000],
                "sources": [url for url, _ in excerpts],
                "topics": [],
                "source_kind": "official_disclosure_search_excerpt",
            }
        except (ResearchError, ValueError, TypeError, AttributeError):
            return None
    else:
        return None
    value["retrieved_at"] = now().isoformat()
    value["sha256"] = hashlib.sha256(value["description"].encode()).hexdigest()
    return value


def suggested_ids(context, dimensions, market, code):
    """Reviewed topic routing only; not empirical correlations or investment advice."""
    by_name = {d["name"]: d for d in dimensions}
    names = context.get("topics", []) if context else []
    result = [
        by_name[name]["id"] for name in names if name in by_name and by_name[name].get("series_code") != code
    ]
    if market in by_name and by_name[market].get("series_code") != code:
        result.append(by_name[market]["id"])
    return list(dict.fromkeys(result))[:8]
