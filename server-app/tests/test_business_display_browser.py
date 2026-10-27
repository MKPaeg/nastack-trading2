"""Saved text display and layout never start research or leak stale-owner responses."""
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_business_display_owner_races_and_auto_height(role):
    root = Path(__file__).resolve().parents[1]
    source = (root / "app.js").read_text()
    helper = source[source.index("async function renderSavedBusiness("):source.index("function renderDetailAnalysis(")]
    nodes = source[source.index("function node("):source.index("function showToast(")]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1000, "height": 800})
        page.set_content('<section id="saved" class="saved-business-description"></section><section class="live-panel news-model-panel"><div class="news-stock-row">종목 선택</div><textarea readonly aria-label="뉴스 분석 종목 사업 정보"></textarea><section class="news-financials">공시 재무상태</section></section>')
        page.add_style_tag(content=(root / "live.css").read_text())
        page.add_script_tag(content=f"""
          let state={{user:{{id:'a',tenant_id:'one',role:'{role}'}}}},sequence=0;
          const pending=[],calls=[];
          function requestIdentifier(){{return String(++sequence);}}
          function api(path){{calls.push(path);return new Promise(resolve=>pending.push(resolve));}}
          {nodes}
          {helper}
          const area=document.querySelector('#saved');
          renderSavedBusiness(area,'005930');
        """)
        # Same user's old stock request must not replace the newly selected stock.
        page.evaluate("renderSavedBusiness(area,'328130');pending[1]({context:{description:'새 종목 사업설명',sources:['https://example.com','javascript:alert(1)']}})")
        expect(page.get_by_text("새 종목 사업설명", exact=True)).to_be_visible()
        page.evaluate("pending[0]({context:{description:'이전 종목 응답'}})")
        expect(page.get_by_text("이전 종목 응답", exact=True)).to_have_count(0)
        assert page.locator("#saved a").count() == 1
        # Account switch within a tenant and between tenants blocks stale results.
        for tenant in ("one", "two"):
            page.evaluate("""tenant=>{
              state.user={id:'a',tenant_id:'one'};renderSavedBusiness(area,'005930');
              state.user={id:'b',tenant_id:tenant};pending.at(-1)({context:{description:'다른 계정의 비공개 설명'}});
            }""", tenant)
            expect(page.get_by_text("다른 계정의 비공개 설명", exact=True)).to_have_count(0)
        # The production resize helper expands AND shrinks when the stock changes.
        page.evaluate(r"""()=>{
          const field=document.querySelector('textarea');
          field.value=('긴 사업설명입니다. 제품과 고객, 시장을 설명합니다.\n').repeat(45);
          resizeBusinessDescription(field);
        }""")
        field = page.get_by_label("뉴스 분석 종목 사업 정보")
        large = field.evaluate("el=>({height:el.clientHeight,scroll:el.scrollHeight})")
        assert large["height"] >= large["scroll"] and large["height"] > 300
        assert page.locator(".news-stock-row").evaluate("el=>getComputedStyle(el).borderTopWidth") == "0px"
        assert page.locator(".news-financials").evaluate("el=>getComputedStyle(el).borderTopWidth") == "0px"
        page.screenshot(path=f"/private/tmp/nastack-business-display-{role}.png", full_page=True)
        page.evaluate("const field=document.querySelector('textarea');field.value='짧은 설명';resizeBusinessDescription(field)")
        assert field.evaluate("el=>el.clientHeight") < large["height"]
        assert page.evaluate("calls.every(path=>path.startsWith('/business-research?code='))")
        browser.close()
