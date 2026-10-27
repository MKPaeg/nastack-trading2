"""Settings layout and single-model policy behavior without external calls."""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def test_email_list_fields_are_sequential_and_fit_small_screens():
    app = (ROOT / "app.js").read_text()
    nodes = app[app.index("function node("):app.index("function showToast(")]
    trade = (ROOT / "trade-ui.js").read_text()
    email_ui = trade[trade.index("async function renderEmailLists("):trade.index("async function renderTradeMonitor(")]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width in (390, 1200):
            page = browser.new_page(viewport={"width": width, "height": 850})
            page.set_content('<main class="panel live-panel" style="width:100%;box-sizing:border-box"><div id="area"></div></main>')
            for stylesheet in ("styles.css", "live.css", "mobile.css"):
                page.add_style_tag(path=str(ROOT / stylesheet))
            page.add_script_tag(content=f"""
                const state={{user:{{role:'admin'}}}};
                {nodes}
                {email_ui}
                const calls=[];let savedName='예시 목록';
                async function api(path, options) {{
                  calls.push({{path, options}});
                  if(options?.method==='PUT'){{savedName=JSON.parse(options.body).name;return {{id:'list-1'}};}}
                  return {{smtp_configured:true,items:[{{id:'list-1',name:savedName,emails:['one@example.test'],trade_notifications:false,version:1}}]}};
                }}
                renderEmailLists(document.querySelector('#area'));
            """)
            page.get_by_label("수신 목록 선택").select_option("list-1")
            fields = page.locator(".email-list-fields > *")
            assert fields.count() == 4
            boxes = [fields.nth(index).bounding_box() for index in range(4)]
            assert all(boxes[index + 1]["y"] >= boxes[index]["y"] + boxes[index]["height"] + 15 for index in range(3))
            assert page.locator(".email-compose").bounding_box()["y"] > fields.last.bounding_box()["y"]
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            page.get_by_label("목록 이름").fill("저장된 목록")
            page.get_by_role("button", name="이메일 목록 저장").click()
            expect(page.get_by_role("status")).to_contain_text("이메일 목록 저장 완료")
            expect(page.get_by_label("목록 이름")).to_have_value("저장된 목록")
            assert page.evaluate("calls.some(call => call.options?.method==='PUT' && call.path.includes('/admin/email-lists/'))")
            page.evaluate("state.user.role='viewer';document.querySelector('#area').replaceChildren();renderEmailLists(document.querySelector('#area'))")
            expect(page.get_by_label("목록 이름")).to_be_disabled()
            expect(page.get_by_role("button", name="이메일 목록 저장")).to_be_disabled()
            expect(page.get_by_role("button", name="이메일 발송", exact=True)).to_be_disabled()
            page.close()
        browser.close()


def test_attribute_policy_uses_saved_model_without_second_selector():
    app = (ROOT / "app.js").read_text()
    nodes = app[app.index("function node("):app.index("function showToast(")]
    default_ui = app[app.index("async function renderDefaultAttributesPolicy("):app.index("async function renderUsers(")]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content('<section id="area" class="panel live-panel"></section>')
        page.add_script_tag(content=f"""
            {nodes}
            {default_ui}
            const requests=[];
            async function api(path,options){{
              requests.push({{path,options}});
              if(path==='/user/vector-defaults')return {{version:1,enabled:true,allow_cloud:false,research_business:false,
                model:{{id:'openai:gpt-test',provider:'openai',label:'OpenAI · gpt-test'}},openai_key_configured:true}};
              return {{total:0,ready:0,pending:0,failed:0,manual_preserved:0}};
            }}
            function reportError(error){{throw error;}}
            renderDefaultAttributesPolicy(document.querySelector('#area'));
        """)
        expect(page.locator("#area")).to_contain_text("사용 모델: OpenAI · gpt-test")
        assert page.locator("#area select").count() == 0
        page.get_by_role("button", name="속성 자동 생성 정책 저장").click()
        assert page.evaluate("requests.some(request => request.options?.method==='PUT' && !Object.hasOwn(JSON.parse(request.options.body),'model'))")
        browser.close()


def test_openai_shows_only_one_model_selector():
    app = (ROOT / "app.js").read_text()
    markup = app.split('section.innerHTML = `', 1)[1].split('`;', 1)[0]
    sync = app[app.index("  function syncModelFields()"):app.index("  async function loadSettingsModels(")]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(markup)
        page.add_style_tag(path=str(ROOT / "live.css"))
        page.add_script_tag(content=f"""
            const form=document.querySelector('#settingsForm');
            {sync}
            form.elements.model_id.append(new Option('gpt-test','gpt-test'));
            form.elements.cloud_model.append(new Option('gpt-test','gpt-test'));
            form.elements.provider.value='openai';form.elements.model_id.value='gpt-test';
            syncModelFields();
        """)
        assert page.locator("#cloudModelField").is_hidden()
        assert page.locator('[name="cloud_model"]').input_value() == "gpt-test"
        page.evaluate("form.elements.provider.value='vllm';syncModelFields()")
        assert page.locator("#cloudModelField").is_visible()
        browser.close()


def test_theme_and_analysis_model_have_separate_cards_and_usage_follows_prompts():
    app = (ROOT / "app.js").read_text()
    markup = app.split('section.innerHTML = `', 1)[1].split('`;', 1)[0]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 850})
        page.set_content(f'<main class="panel live-panel">{markup}</main>')
        for stylesheet in ("styles.css", "live.css", "mobile.css"):
            page.add_style_tag(path=str(ROOT / stylesheet))
        cards = page.locator(".settings-form > .settings-card")
        assert cards.count() == 2
        assert cards.nth(0).bounding_box()["y"] + cards.nth(0).bounding_box()["height"] + 19 <= cards.nth(1).bounding_box()["y"]
        assert cards.nth(0).evaluate("el => getComputedStyle(el).borderTopWidth") != "0px"
        assert cards.nth(1).evaluate("el => getComputedStyle(el).borderTopWidth") != "0px"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert app.index('const tasks=panel("LLM 작업별 역할·프롬프트")') < app.index('renderLLMUsagePanel(section).catch(reportError);')
        browser.close()
