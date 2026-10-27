"""Actual browser rendering of personal credentials; all API calls mocked."""

from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
@pytest.mark.parametrize("service,title", [("ecos", "한국은행 ECOS · 환율"), ("krx", "KRX Open API · 지수 데이터")])
def test_ecos_registration_restore_roles_and_account_switch(role, service, title):
    root = Path(__file__).resolve().parents[1]
    source = (root / "trade-ui.js").read_text().split("async function renderPersonalConnections(", 1)[1]
    helper = "async function renderPersonalConnections(" + source
    nodes = (root / "app.js").read_text().split("function node(", 1)[1].split("function showToast(", 1)[0]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content('<main id="area"></main>')
        page.add_style_tag(content=(root / "styles.css").read_text())
        page.add_style_tag(content=(root / "live.css").read_text())
        page.add_script_tag(
            content=f"""
          let state={{user:{{id:'a',tenant_id:'one',role:'{role}',email:'test@example.com'}}}},saved=false;
          const calls=[];
          function node({nodes}
          function panel(title){{const el=node('section',undefined,'panel live-panel');el.append(node('h2',title));return el;}}
          async function buttonTask(button,label,work){{return work();}}
          async function api(path,options){{calls.push([path,options]);if(path.endsWith('/krx/test'))return {{latest_date:'2026-10-26',latest_value:23.45,count:1}};if(path.endsWith('/ecos/test'))return {{latest_date:'2026-10-26',latest_value:1324.3,count:6}};if(options){{saved=true;return {{version:1,enabled:true,configured:true,secret_configured:true,fields:{{}},smtp_hosts:['smtp.gmail.com']}};}}return {{version:saved?1:0,enabled:saved,configured:saved,secret_configured:saved,fields:{{}},smtp_hosts:['smtp.gmail.com']}};}}
          {helper}
          const area=document.querySelector('#area');renderPersonalConnections(area);
        """
        )
        block = page.locator("details").filter(has=page.get_by_text(title, exact=True))
        expect(block).to_have_count(1)
        block.locator("summary").click()
        field = page.get_by_label(title + " API 키")
        assert field.get_attribute("type") == "password"
        expect(field).to_have_value("")
        if role == "viewer":
            expect(field).to_be_disabled()
            expect(block.get_by_role("button", name="개인 연결 저장")).to_be_disabled()
            expect(block.get_by_role("button", name="저장된 키로 연결 테스트")).to_be_disabled()
        else:
            field.fill("browser-synthetic-key")
            block.get_by_label("개인 연결 사용").check()
            block.get_by_role("button", name="개인 연결 저장").click()
            expect(block.get_by_text("암호화 저장 완료", exact=False)).to_be_visible()
            expect(field).to_have_value("")
            block.get_by_role("button", name="저장된 키로 연결 테스트").click()
            expect(block.get_by_text("KRX 연결 성공 · VKOSPI 23.45" if service == "krx" else "연결 성공 · 최신 관측일 2026-10-26", exact=False)).to_be_visible()
            page.evaluate("area.replaceChildren();renderPersonalConnections(area)")
            expect(page.get_by_label(title + " API 키")).to_have_value("")
            expect(page.get_by_text("개인 연결 등록됨 · 비밀값 저장됨", exact=False).first).to_be_attached()
        assert page.evaluate("service=>calls.filter(c=>c[1]).every(c=>c[0]===`/user/connections/${service}` || c[0]===`/user/connections/${service}/test`)", service)
        if service == "krx":
            block.evaluate("e=>e.open=true")
            expect(block.get_by_role("button", name="저장된 키로 연결 테스트")).to_have_count(1)
            expect(block.get_by_text("실제 VKOSPI 행을 확인하세요.", exact=False)).to_be_visible()
            assert block.locator(".connection-fields").evaluate("e=>parseFloat(getComputedStyle(e).rowGap)") >= 16
            assert block.locator(".connection-actions").evaluate("e=>parseFloat(getComputedStyle(e).gap)") >= 12
            if role == "trader":
                page.screenshot(path="/private/tmp/nastack-krx-registration.png", full_page=True)
            page.set_viewport_size({"width":390,"height":900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        # A response from A must not attach to B's screen, even in the same tenant.
        for tenant in ["one", "two"]:
            page.evaluate(
                """tenant=>{
              area.replaceChildren();state.user={id:'a',tenant_id:'one',role:'trader',email:'a@example.com'};
              api=()=>new Promise(resolve=>window.finish=resolve);renderPersonalConnections(area);
              state.user={id:'b',tenant_id:tenant,role:'trader',email:'b@example.com'};
              finish({version:1,enabled:true,configured:true,secret_configured:true,fields:{},smtp_hosts:[]});
            }""",
                tenant,
            )
            expect(page.locator("details")).to_have_count(0)
        browser.close()
