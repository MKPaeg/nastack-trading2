from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_explicit_llm_actions_show_usage_cost_and_keep_session_refresh_alive(role):
    root = Path(__file__).resolve().parents[1]
    app = (root / "app.js").read_text()
    nodes = app[app.index("function node("):app.index("function showToast(")]
    busy = app[app.index("async function buttonTask("):app.index("async function renderDimensionData(")]
    helpers = (root / "collection-ui.js").read_text().split("/* Server-owned incremental collection", 1)[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 800})
        page.set_content('<main><button id="action" class="primary">합성 LLM 작업</button></main>')
        for name in ("styles.css", "live.css"):
            page.add_style_tag(path=str(root / name))
        page.add_script_tag(content=f"""
          let state={{user:{{id:'a',tenant_id:'one',role:'{role}'}},sessionEpoch:1}},calls=0,finish,fail,mode='success';
          {nodes}
          {busy}
          {helpers}
          const usage={{llm_calls:1,known:true,cost_known:true,total_tokens:1200,input_tokens:1000,output_tokens:200,cached_input_tokens:0,estimated_usd:.008,priced_calls:1}};
          async function api(path,options){{calls++;return new Promise((resolve,reject)=>{{
            finish=()=>{{options.onUsage?.(usage);resolve({{status:'completed'}});}};
            fail=()=>reject(Error('모의 통신 실패'));
          }});}}
          const button=document.querySelector('#action');
          button.onclick=()=>llmButtonTask(button,'LLM 실행 중…',()=>button.isConnected,call=>call('/synthetic',{{method:'POST'}})).catch(()=>{{}});
        """)
        button = page.locator("#action")
        button.click()
        status = page.locator(".llm-action-status")
        if role == "viewer":
            expect(status).to_have_count(0)
            assert page.evaluate("calls") == 0
        else:
            expect(button).to_have_attribute("aria-busy", "true")
            page.evaluate("state.user={...state.user}")
            expect(status).to_contain_text("경과 0분 01초")
            button.dispatch_event("click")
            assert page.evaluate("calls") == 1
            page.evaluate("finish()")
            expect(status).to_contain_text("1,200개")
            expect(status).to_contain_text("입력 1,000 / 출력 200")
            expect(status).to_contain_text("US$0.008000")
            expect(status).to_have_attribute("aria-busy", "false")
            expect(button).to_be_enabled()
            assert status.evaluate("el=>el.scrollWidth<=el.clientWidth")
            page.screenshot(path=f"/private/tmp/nastack-llm-cost-{role}.png", full_page=True)
            button.click()
            page.evaluate("fail()")
            expect(status).to_contain_text("사용량 미보고")
            assert "US$0.000000" not in status.inner_text()
            expect(button).to_be_enabled()
            for tenant in ("one", "two"):
                page.evaluate("state.user={id:'a',tenant_id:'one',role:'trader'}")
                button.click()
                before = status.inner_text()
                page.evaluate("tenant=>{state.user={id:'b',tenant_id:tenant,role:'trader'};finish();}", tenant)
                page.wait_for_timeout(1100)
                assert status.inner_text() == before
                # New session/screen: never reuse another account's presentation.
                page.evaluate("state.user={id:'a',tenant_id:'one',role:'trader'};state.sessionEpoch++;button.disabled=false;button.dataset.working='false'")
        browser.close()
