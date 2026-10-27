"""Actual Chromium UI with synthetic policy only; no keys/network/order calls."""
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_opendart_registration_checks_restore_and_roles(role):
    root = Path(__file__).resolve().parents[1]
    helper = "async function renderPersonalConnections(" + (root / "trade-ui.js").read_text().split("async function renderPersonalConnections(", 1)[1]
    nodes = (root / "app.js").read_text().split("function node(", 1)[1].split("function showToast(", 1)[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.set_content('<main id="area"></main>')
        page.add_style_tag(content=(root / "styles.css").read_text())
        page.add_style_tag(content=(root / "live.css").read_text())
        page.add_script_tag(content=f"""
        let state={{user:{{id:'a',tenant_id:'one',role:'{role}',email:'synthetic@example.com'}}}};
        let saved=false,last=null;const calls=[];
        function node({nodes}
        function panel(title){{const e=node('section',undefined,'panel live-panel');e.append(node('h2',title));return e;}}
        async function buttonTask(button,label,work){{const old=button.textContent;button.textContent=label;button.disabled=true;try{{return await work();}}finally{{button.textContent=old;button.disabled=false;}}}}
        async function api(path,options){{calls.push([path,options]);
          if(path.endsWith('/financial-check')){{last={{status:'available',company_name:'삼성전자',code:'005930',year:2025,fs_div:'CFS',report:'11011',account_count:40,statements:['재무상태표','현금흐름표']}};return last;}}
          if(path.endsWith('/opendart/test'))return {{status:'connected'}};
          if(options)saved=true;
          return {{version:saved?1:0,enabled:saved,configured:saved,secret_configured:saved,fields:{{}},checks:last?{{opendart_financial:last}}:{{}},smtp_hosts:[]}};
        }}
        {helper}
        const area=document.querySelector('#area');renderPersonalConnections(area);
        """)
        block = page.locator("details").filter(has=page.get_by_text("OpenDART · 공시·재무자료", exact=True))
        expect(block).to_have_count(1)
        expect(page.locator("details.service-connection")).to_have_count(6)
        assert page.locator(".paper-panel").count() == 0
        cards = page.locator("details.service-connection").evaluate_all("""items=>items.map((e,i)=>{
          const s=getComputedStyle(e),r=e.getBoundingClientRect();
          return {closed:!e.open,border:parseFloat(s.borderTopWidth),padding:parseFloat(s.paddingTop),
            gap:i?r.top-items[i-1].getBoundingClientRect().bottom:20,height:r.height};
        })""")
        assert all(c['closed'] and c['border']>=1 and c['padding']>=16 and c['gap']>=20 and c['height']>=60 for c in cards)
        if role == "trader":
            page.screenshot(path="/private/tmp/nastack-connections-closed-cards.png", full_page=True)
        block.locator("summary").click()
        expect(page.locator("details.service-connection")).to_have_count(6)
        assert page.locator('[data-service="opendart"]').evaluate("e=>parseFloat(getComputedStyle(e).marginTop)") >= 20
        assert block.locator(".connection-actions").evaluate("e=>parseFloat(getComputedStyle(e).gap)") >= 12
        assert block.locator(".connection-fields").evaluate("e=>parseFloat(getComputedStyle(e).rowGap)") >= 16
        key = block.get_by_label("OpenDART · 공시·재무자료 API 키", exact=True)
        expect(key).to_have_attribute("type", "password")
        expect(key).to_have_value("")
        if role == "viewer":
            expect(key).to_be_disabled()
            expect(block.get_by_role("button", name="개인 연결 저장")).to_be_disabled()
            expect(block.get_by_role("button", name="저장된 키로 연결 테스트")).to_be_disabled()
            expect(block.get_by_role("button", name="재무자료 제공 확인")).to_be_disabled()
        else:
            key.fill("a" * 40)
            block.get_by_label("개인 연결 사용").check()
            block.get_by_role("button", name="개인 연결 저장").click()
            expect(key).to_have_value("")
            assert page.evaluate("calls.filter(c=>c[1]).length") == 1
            block.get_by_role("button", name="저장된 키로 연결 테스트").click()
            expect(block.get_by_text("OpenDART 연결 성공", exact=False)).to_be_visible()
            block.get_by_label("재무 확인 종목코드").fill("005930")
            block.get_by_label("사업연도", exact=True).fill("2025")
            block.get_by_role("button", name="재무자료 제공 확인").click()
            expect(block.get_by_text("재무자료 제공 확인 ·", exact=False)).to_contain_text("40개 계정")
            expect(block.get_by_text("재무자료 제공 확인 ·", exact=False)).to_contain_text("시계열 저장 없음")
            page.evaluate("area.replaceChildren();renderPersonalConnections(area)")
            expect(page.get_by_label("OpenDART · 공시·재무자료 API 키")).to_have_value("")
            expect(page.get_by_label("재무 확인 종목코드")).to_have_value("005930")
            assert page.evaluate("calls.filter(c=>c[1]).every(c=>c[0].startsWith('/user/connections/opendart'))")
            if role == "trader":
                block.locator("summary").click()
                page.evaluate("document.querySelectorAll('.service-connection').forEach(e=>e.open=true)")
                page.screenshot(path="/private/tmp/nastack-opendart-ui.png", full_page=True)
                page.set_viewport_size({"width": 390, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path="/private/tmp/nastack-connections-mobile.png", full_page=True)
        # A late response must not be attached after a same-/cross-tenant switch.
        for tenant in ("one", "two"):
            page.evaluate("""tenant=>{area.replaceChildren();state.user={id:'a',tenant_id:'one',role:'trader',email:'a@example.com'};
            api=()=>new Promise(resolve=>window.finish=resolve);renderPersonalConnections(area);
            state.user={id:'b',tenant_id:tenant,role:'trader',email:'b@example.com'};
            finish({version:0,fields:{},smtp_hosts:[]});} """, tenant)
            expect(page.locator("details")).to_have_count(0)
        browser.close()
