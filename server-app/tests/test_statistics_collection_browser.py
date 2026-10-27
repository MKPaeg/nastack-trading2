"""Statistics collection controls stay scoped and report observed LLM usage."""

import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def test_statistics_selected_and_all_actions_show_progress_cost_and_limit():
    root = Path(__file__).resolve().parents[1]
    app = (root / "app.js").read_text()
    nodes = app[app.index("function node("):app.index("function showToast(")]
    busy = app[app.index("async function buttonTask("):app.index("async function renderDimensionData(")]
    statistics = app[app.index("function seoulDate("):app.index("function holdingPurchaseReturn(")]
    helpers = (root / "collection-ui.js").read_text().split("// Opt-in for LLM buttons only", 1)[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 800, "height": 700})
        page.set_content('<main id="statistics"></main>')
        page.add_style_tag(path=str(root / "styles.css"))
        page.add_style_tag(path=str(root / "live.css"))
        page.add_script_tag(content=f"""
          let state={{user:{{id:'a',tenant_id:'one',role:'trader'}},sessionEpoch:1,view:'data'}};
          {nodes}
          {busy}
          {helpers}
          function panel(title){{const item=node('section',undefined,'panel');item.append(node('h2',title));return item;}}
          function formatTime(value){{return value;}}
          function dimensionChart(){{const item=node('div');item.append(node('svg'));return item;}}
          const usage={{llm_calls:0,known:true,cost_known:true,cache_write_known:true,
            total_tokens:0,input_tokens:0,output_tokens:0,cached_input_tokens:0,
            cache_write_tokens:0,estimated_usd:0,priced_calls:0,model_usage:[]}};
          const pending=[],requested=[];window.confirm=()=>true;
          async function api(path,options={{}}){{
            if(path==='/statistics')return {{note:'저장 통계',sources:[],series:['ONE','TWO','THREE'].map(id=>({{
              id,name:id,source:'공식',frequency:'월간',unit:'단위',url:'https://example.com',points:[]
            }}))}};
            requested.push(path);
            return new Promise((resolve,reject)=>pending.push({{
              resolve:()=>resolve({{cached:false,count:3,request_usage:usage}}),
              limit:()=>{{const error=Error('오늘의 수집 시도 한도에 도달했습니다.');error.status=429;error.usage=usage;reject(error);}}
            }}));
          }}
          {statistics}
          renderStatistics(document.querySelector('#statistics'));
        """)
        selected = page.locator("#sourceStatistics .source-row button").nth(0)
        all_button = page.locator("#sourceStatistics .source-row button").nth(1)
        expect(selected).to_be_enabled()
        expect(all_button).to_be_enabled()
        selected.click()
        expect(selected).to_have_attribute("aria-busy", "true")
        expect(selected).to_have_class(re.compile("task-busy"))
        assert page.evaluate("requested.length") == 1
        page.evaluate("pending[0].resolve()")
        expect(page.get_by_text("ONE 수집 완료", exact=False)).to_contain_text("토큰 0개")
        expect(page.get_by_text("ONE 수집 완료", exact=False)).to_contain_text("US$0.000000")
        expect(selected).to_be_enabled()
        all_button.click()
        expect(all_button).to_have_attribute("aria-busy", "true")
        assert page.evaluate("requested.length") == 2
        page.evaluate("pending[1].resolve()")
        expect(page.get_by_text("진행 1/3", exact=False)).to_be_visible()
        page.evaluate("pending[2].limit()")
        expect(page.get_by_text("중단 2/3", exact=False)).to_contain_text("토큰 0개")
        assert page.evaluate("requested.length") == 3
        expect(all_button).to_be_enabled()
        page.screenshot(path="/private/tmp/nastack-statistics-collection-buttons.png", full_page=True)
        page.evaluate("state.user={id:'a',tenant_id:'one',role:'viewer'}")
        page.get_by_role("button", name="통계 DB 새로고침").click()
        expect(selected).to_be_disabled()
        expect(all_button).to_be_disabled()
        page.evaluate("state.user={id:'a',tenant_id:'one',role:'trader'}")
        page.get_by_role("button", name="통계 DB 새로고침").click()
        expect(all_button).to_be_enabled()
        all_button.click()
        expect(all_button).to_have_attribute("aria-busy", "true")
        page.evaluate("state.user={id:'a',tenant_id:'two',role:'trader'};pending[3].resolve()")
        page.wait_for_timeout(100)
        assert page.evaluate("requested.length") == 4  # No next owner-scoped request.
        browser.close()
