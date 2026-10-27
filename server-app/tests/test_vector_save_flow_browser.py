"""Production save controls in a real DOM; no broker, paid or production writes."""
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_save_flow_reasons_order_and_stale_response(role):
    source = (Path(__file__).resolve().parents[1] / "app.js").read_text()
    nodes = source[source.index("function node("):source.index("function showToast(")]
    controls = source[source.index('  const attributeSave=node("button","종목 속성으로 저장"'):source.index('  const charts = node("div",undefined,"vector-trend-charts")')]
    save = source[source.index('    const selectedIds=()=>'):source.index('for(const warning of data.warnings)')]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content('<main id="preview"></main>')
        page.add_style_tag(path=str(Path(__file__).resolve().parents[1] / "live.css"))
        page.add_script_tag(content=nodes + f"""
          const preview=document.querySelector('#preview');
          const state={{user:{{id:'a',role:'{role}'}}}};
          const item={{code:'101680'}},activeCatalog={{items:[{{id:'sector-01',family:'sector',name:'기계'}}]}};
          const vectorDraft={{selected:new Set(['sector:기계']),start:'2026-01-01',end:'2026-10-26',window:'60',horizons:[1]}};
          let requestVersion=1,owner='a',calls=0,pending;
          const valid=()=>owner===state.user.id;
          const attributeStatus=node('p'),retryDefaults=node('button');
          function factorProfileView(){{return node('p','저장 속성');}}
          async function api(){{calls++;return new Promise(resolve=>pending=resolve);}}
          {controls}
          preview.append(node('div','상세 차트'));
          const s={{samples:0,required_samples:20}};
          const data={{instrument:{{code:'101680',name:'테스트 종목'}},status:'insufficient_data',persistence:{{saved:true,id:'result-a'}},request:{{code:'101680',dimensions:['sector-01'],start:vectorDraft.start,end:vectorDraft.end,window:60,horizons:[1]}}}};
          function show(){{{save}}}
          show();
        """)
        button = page.get_by_role("button", name="종목 속성으로 저장", exact=True)
        expect(button).to_be_disabled()
        expect(page.get_by_role("status")).to_contain_text("조회 전용" if role == "viewer" else "유효 0쌍 / 최소 20쌍")
        assert page.evaluate("calls") == 0
        assert page.evaluate("preview.firstElementChild.contains(attributeSave)")
        page.evaluate("data.status='completed';s.samples=30;show()")
        if role == "viewer":
            expect(button).to_be_disabled()
        else:
            expect(button).to_be_enabled()
            expect(page.get_by_role("status")).to_contain_text("저장 가능")
            button.click()
            expect(button).to_be_disabled()
            expect(page.get_by_role("status")).to_contain_text("저장 중")
            page.evaluate("pending({selected_dimensions:['sector-01']})")
            expect(page.get_by_role("status")).to_contain_text("저장 완료")
            page.evaluate("vectorDraft.end='2026-10-23';show()")
            expect(button).to_be_disabled()
            expect(page.get_by_role("status")).to_contain_text("다른 결과")
            page.evaluate("vectorDraft.end='2026-10-26';show()")
            button.click()
            page.evaluate("requestVersion++;attributeSave.disabled=true;attributeNote.textContent='저장 대기 · 새로운 조건 분석 중';pending({selected_dimensions:['sector-01']})")
            expect(page.get_by_role("status")).to_contain_text("새로운 조건 분석 중")
            expect(button).to_be_disabled()
            page.evaluate("show()")
            button.click()
            page.evaluate("state.user.id='b';attributeNote.textContent='계정 전환';pending({selected_dimensions:['sector-01']})")
            expect(page.get_by_role("status")).to_contain_text("계정 전환")
        page.evaluate("owner=state.user.id;data.status='insufficient_data';show()")
        page.screenshot(path=f"/private/tmp/nastack-vector-save-flow-{role}.png")
        browser.close()


def test_change_invalidates_save_and_debounces_analysis():
    source = (Path(__file__).resolve().parents[1] / "app.js").read_text()
    update = source[source.index("  function update() {", source.index("async function analyzeNow")):]
    assert "attributeSave.disabled=true;attributeSave.onclick=null;attributeView.replaceChildren();" in update
    assert "setTimeout(()=>analyzeNow(version),300)" in update
    assert "saveActions.after(actions)" in source
    refresh_start = source.index('  refreshStock.addEventListener')
    refresh = source[refresh_start:source.index('  stockChoice.addEventListener', refresh_start)]
    assert 'attributeSave.disabled=true;attributeSave.onclick=null;' in refresh
    assert "분석 이력 DB 저장 (종목 속성 확정과 별개)" in source
