"""Context guide tracks every primary view and the trading/collection subtabs."""

from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def test_screen_help_tracks_navigation_and_subtabs_without_external_calls():
    app = (ROOT / "app.js").read_text()
    nodes = app[app.index("function node("):app.index("function showToast(")]
    help_script = (ROOT / "screen-help.js").read_text()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_content('<section id="liveContent"></section><aside><div id="liveInspector"></div></aside>')
        page.add_script_tag(content=f"""
          const $=selector=>document.querySelector(selector);
          {nodes}
          const state={{user:{{id:'owner-1',tenant_id:'tenant-1'}},view:'orders',tradingTab:'orders',dataCollectionTab:'status',analysisTab:'detail',holdingsTab:'positions'}};
          const persistScreenState=()=>{{}};
          {help_script}
        """)
        views = {
            "dashboard": "대시보드", "watchlist": "관심종목", "recommendations": "추천종목",
            "holdings": "보유종목", "orders": "종목 매매", "reports": "분석 리포트",
            "logs": "시스템 로그", "settings": "사용자 설정", "users": "사용자 관리",
        }
        for view, heading in views.items():
            page.evaluate("view => {state.view=view;updateScreenHelp()}", view)
            assert page.locator("#screenHelp h2").inner_text() == heading
        page.evaluate("state.view='orders';state.tradingTab='realized';updateScreenHelp()")
        assert page.locator("#screenHelp h2").inner_text() == "종목 매매 · 실현손익"
        for tab, heading in {
            "status": "데이터 수집·갱신", "business": "사업 설명 재조사",
            "statistics": "통계 데이터", "news": "뉴스·공시 수집 계획",
            "dimensions": "다차원 데이터 수집",
        }.items():
            page.evaluate("tab => {state.view='data';state.dataCollectionTab=tab;updateScreenHelp()}", tab)
            assert page.locator("#screenHelp h2").inner_text() == heading
        for tab, heading in {
            "detail": "종목 상세", "dimensions": "다차원 데이터",
            "vectors": "트레이딩 벡터", "sources": "뉴스 및 통계 분석",
        }.items():
            page.evaluate("tab => {state.view='analysis';state.analysisTab=tab;updateScreenHelp()}", tab)
            assert page.locator("#screenHelp h2").inner_text() == heading
        page.evaluate("state.view='holdings';state.holdingsTab='returns';updateScreenHelp()")
        assert page.locator("#screenHelp h2").inner_text() == "기간별 수익률"
        page.evaluate("state.view='orders';state.tradingTab='orders';updateScreenHelp()")
        help_text = page.locator("#screenHelp").inner_text()
        assert "기존 보유수량이 있어도 가격 조건 충족 시" in help_text
        assert "전체 일시정지 후 재개" in help_text
        browser.close()
