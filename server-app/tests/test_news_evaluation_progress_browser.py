"""Exercise production evaluation controls with delayed, entirely mocked work."""
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_news_evaluation_animation_elapsed_results_and_owner_isolation(role):
    root = Path(__file__).resolve().parents[1]
    app = (root / "app.js").read_text()
    nodes = app[app.index("function node("):app.index("function showToast(")]
    button_task = app[app.index("async function buttonTask("):app.index("async function renderDimensionData(")]
    screen = app[app.index("async function renderDailyNews("):app.index("function seoulDate(")]
    progress = (root / "collection-ui.js").read_text().split("/* Server-owned incremental collection", 1)[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_content('<main id="main" class="live-panel"></main>')
        page.add_style_tag(path=str(root / "styles.css"))
        page.add_style_tag(path=str(root / "live.css"))
        page.add_script_tag(content=f"""
          let state={{user:{{id:'a',tenant_id:'one',role:'{role}'}},newsCode:'123456'}},calls=0,finish,fail;
          const requests=[];
          {nodes}
          function panel(title){{const area=node('section');area.append(node('h3',title));return area;}}
          const data={{enabled:true,local_model_configured:true,levels:5,window_days:14,timezone:'Asia/Seoul',
            start_date:'2026-10-14',end_date:'2026-10-27',articles:[],assessments:[],aggregate:[],runs:[],
            sources:[{{id:'source',kind:'news',eligible:true,connected:true,name:'검증용 출처',reference_url:'https://example.com'}}]}};
          async function api(path,options){{requests.push([path,options?.method||'GET']);
            if(path==='/vectors/instruments')return {{items:[{{code:'123456',name:'검증 종목'}}]}};
            if(path.startsWith('/news/models'))return {{selected:'default',default:{{label:'모의 모델'}},default_source:'모의 연결',items:[],cloud_enabled:true}};
            if(path.startsWith('/news/today'))return data;
            throw Error('Unexpected request: '+path);
          }}
          function loadWatchMembership(){{return Promise.resolve({{}});}}
          function watchlistAddControl(){{return node('span');}}
          function resizeBusinessDescription(){{}}
          function factorProfileView(){{return node('div');}}
          function impactTotalLabel(){{return '미평가';}}
          function newsSourceSummary(){{return '저장됨';}}
          function formatTime(v){{return String(v);}}
          function reportError(e){{throw e;}}
          async function renderStatistics(){{}}
          function refreshBusinessCollection(code,notify,retry,valid){{
            calls++;notify('뉴스 영향 평가 · 1/3 묶음');
            return new Promise((resolve,reject)=>{{finish=resolve;fail=reject;}});
          }}
          {button_task}
          {progress}
          {screen}
          renderDailyNews(document.querySelector('#main'));
        """)
        button = page.get_by_role("button", name="저장 자료의 변경·미평가 항목 평가 (최대 30건)", exact=True)
        expect(button).to_be_visible()
        status = page.locator(".evaluation-progress")
        expect(status).to_be_hidden()
        if role == "viewer":
            expect(button).to_be_disabled()
            button.dispatch_event("click")
            assert page.evaluate("calls") == 0
            expect(status).to_be_hidden()
        else:
            page.once("dialog", lambda dialog: dialog.dismiss())
            button.click()
            assert page.evaluate("calls") == 0
            expect(status).to_be_hidden()
            page.on("dialog", lambda dialog: dialog.accept())
            for outcome, label in [("completed", "평가 완료"), ("partial", "평가 일부 완료"), ("failed", "평가 실패"), ("exception", "요청 실패")]:
                previous = page.evaluate("calls")
                button.click()
                running = page.get_by_role("button", name="저장 자료 평가 중…", exact=True)
                expect(running).to_be_disabled()
                expect(running).to_have_attribute("aria-busy", "true")
                expect(status).to_contain_text("뉴스 영향 평가 · 1/3 묶음")
                expect(status).to_contain_text("경과 0분 00초")
                assert running.evaluate("el=>getComputedStyle(el,'::before').animationName") == "spin"
                running.dispatch_event("click")
                assert page.evaluate("calls") == previous + 1
                expect(page.get_by_label("뉴스 평가 전용 모델")).to_be_disabled()
                if outcome == "completed":
                    expect(status.locator(".evaluation-elapsed")).not_to_have_text("경과 0분 00초")
                    # The real app refreshes /me every 30 seconds with a NEW object.
                    # This is not an account switch and must not freeze the timer.
                    before_refresh = status.locator(".evaluation-elapsed").inner_text()
                    page.evaluate("state.user={...state.user}")
                    expect(status.locator(".evaluation-elapsed")).not_to_have_text(before_refresh)
                    page.set_viewport_size({"width": 390, "height": 900})
                    assert status.evaluate("el=>el.scrollWidth<=el.clientWidth")
                    page.screenshot(path=f"/private/tmp/nastack-news-progress-{role}.png", full_page=True)
                    page.set_viewport_size({"width": 1440, "height": 1000})
                page.evaluate("value=>value==='exception'?fail(Error('모의 오류')):finish({status:value})", outcome)
                expect(status).to_contain_text(label)
                expect(status).to_contain_text("소요")
                expect(status).to_have_attribute("aria-busy", "false")
                expect(status.locator(".task-busy")).to_have_count(0)
                expect(button).to_be_enabled()
                expect(page.get_by_label("뉴스 평가 전용 모델")).to_be_enabled()
            for tenant in ("one", "two"):
                button.click()
                before = status.inner_text()
                page.evaluate("tenant=>{state.user={id:'b',tenant_id:tenant,role:'trader'};finish({status:'failed',error:'PRIVATE_RESULT'});}", tenant)
                page.wait_for_timeout(1100)
                assert status.inner_text() == before
                assert "PRIVATE_RESULT" not in page.locator("body").inner_text()
                # Leaving the old screen and mounting a fresh one cannot restore its timer/result.
                page.evaluate("state.user={id:'a',tenant_id:'one',role:'trader'};document.querySelector('#main').replaceChildren();renderDailyNews(document.querySelector('#main'))")
                expect(status).to_be_hidden()
        assert page.evaluate("requests.every(([,method])=>method==='GET')")
        browser.close()
