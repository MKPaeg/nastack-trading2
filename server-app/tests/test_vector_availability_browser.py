from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_only_connected_daily_saved_dimensions_can_be_selected(role):
    source=(Path(__file__).resolve().parents[1]/"app.js").read_text()
    nodes=source[source.index("function node("):source.index("function showToast(")]
    helper=source[source.index("function vectorChoiceState("):source.index("async function renderVectorAnalysis(")]
    filter_fn=next(line for line in source.splitlines() if line.startswith("  function selectableDraft("))
    entry=source.split("    for (const label of candidates) {",1)[1].split("\n    }\n    group.append",1)[0]
    with sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page()
        page.set_content('<main id="grid"></main><p id="status" role="status"></p>')
        page.add_style_tag(path=str(Path(__file__).resolve().parents[1]/"live.css"))
        page.add_script_tag(content=nodes+helper+f"""
          const state={{user:{{id:'a',role:'{role}'}}}},blockedSelectionNames=new Set();
          const activeCatalog={{items:[
            {{name:'연결된 자료',family:'sector',can_collect:true,vector_available:true,stored_observations:100}},
            {{name:'미연결',family:'sector',can_collect:false}},
            {{name:'미수집',family:'sector',can_collect:true,vector_available:false,vector_unavailable_reason:'저장 데이터 없음'}},
            {{name:'월간 지수',family:'sector',can_collect:true,daily_analysis_eligible:false,vector_available:false}},
            {{name:'상태 미확인',family:'sector',can_collect:true}}
          ]}};
          const vectorDraft={{selected:new Set(['sector:연결된 자료','sector:미연결','sector:미수집'])}};
          {filter_fn}
          const key='sector',grid=document.querySelector('#grid'),entries=[];
          let updates=0;
          function update(){{updates++;}}
          function draw(){{grid.replaceChildren();entries.length=0;vectorDraft.selected=selectableDraft(vectorDraft.selected);for(const label of activeCatalog.items.map(i=>i.name)){{{entry}}}document.querySelector('#status').textContent=[...blockedSelectionNames].join(', ');}}
          draw();
        """)
        expect(page.locator('input:not([disabled])')).to_have_count(1)
        expect(page.locator('input[disabled]')).to_have_count(4)
        assert page.evaluate("[...vectorDraft.selected]")==['sector:연결된 자료']
        expect(page.get_by_role('status')).to_contain_text('미연결')
        expect(page.get_by_role('status')).to_contain_text('미수집')
        assert page.evaluate("updates")==0
        page.locator('input:not([disabled])').uncheck()
        assert page.evaluate("vectorDraft.selected.size")==0
        # Even forged change events on disabled controls cannot enter the draft.
        page.evaluate("const input=grid.querySelector('input[disabled]');input.checked=true;input.dispatchEvent(new Event('change'))")
        assert page.evaluate("vectorDraft.selected.size")==0
        # A new account gets only its own availability snapshot, not A's observations.
        page.evaluate("state.user.id='b';activeCatalog.items[0].vector_available=false;activeCatalog.items[0].vector_unavailable_reason='저장 데이터 없음';draw()")
        expect(page.locator('input:not([disabled])')).to_have_count(0)
        page.screenshot(path=f'/private/tmp/nastack-vector-availability-{role}.png')
        browser.close()
