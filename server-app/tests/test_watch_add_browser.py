"""Production add controls: private cached lists, explicit writes, no paid providers."""
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.mark.parametrize("role", ["admin", "trader", "viewer"])
def test_watch_add_eligibility_registration_reuse_and_owner_switch(role):
    root = Path(__file__).resolve().parents[1]
    source = (root / "app.js").read_text()
    helpers = source[source.index("async function loadWatchMembership("):source.index("function openInstrument(")]
    node = source[source.index("function node("):source.index("function showToast(")]
    task = source[source.index("async function buttonTask("):source.index("async function buttonTask(") + source[source.index("async function buttonTask("):].index("\n}\n") + 3]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content('<main id="actions"></main>')
        page.add_style_tag(content=(root / "live.css").read_text())
        page.add_script_tag(content=f"""
          const state={{user:{{id:'a',tenant_id:'one',role:'{role}'}}}},calls=[],added=new Set();
          let registered=false,deferredLookup=null;
          async function api(path,options={{}}){{
            calls.push([path,options.method||'GET',options.body?JSON.parse(options.body):null]);
            if(path.startsWith('/watchlist?page=')){{
              const rows=[...Array.from({{length:10}},(_,i)=>String(i+1).padStart(6,'0')),'005930',...added];
              const page=Number(new URL('http://test'+path).searchParams.get('page'));
              return {{total:rows.length,items:rows.slice((page-1)*10,page*10).map(code=>({{instrument:{{code}}}}))}};
            }}
            if(path==='/portfolio/holdings')return {{snapshot:{{items:[{{code:'000720',quantity:5}},{{code:'034220',quantity:0}}]}}}};
            if(path==='/portfolio/kiwoom/holdings')return {{snapshot:{{items:[{{code:'003550',quantity:2}}]}}}};
            if(path.startsWith('/instruments?q=999999'))return new Promise(resolve=>deferredLookup=resolve);
            if(path.startsWith('/instruments?'))return {{items:registered?[{{code:'328130',id:'owned-registered-id',status:'active'}}]:[]}};
            if(path.startsWith('/instrument-search?'))return {{snapshot:'a'.repeat(64),items:[{{code:'328130',name:'루닛'}}]}};
            if(path.startsWith('/admin/instrument-search/')){{registered=true;return {{code:'328130',id:'owned-registered-id',status:'active'}};}}
            if(path==='/watchlist'&&options.method==='POST'){{added.add('328130');return {{}};}}
            throw Error('Unexpected API '+path);
          }}
          {node}
          {task}
          {helpers}
          function draw(){{
            const area=document.querySelector('#actions');area.replaceChildren();const membership=loadWatchMembership();
            for(const code of ['005930','000720','003550','034220','328130','328130'])area.append(watchlistAddControl({{code,name:code}},membership));
          }}
          draw();
        """)
        if role == "viewer":
            expect(page.get_by_role("button", name="328130 관심종목 추가", exact=True)).to_have_count(0)
            assert page.evaluate("calls.every(([,method])=>method==='GET')")
        else:
            expect(page.get_by_role("button", name="328130 관심종목 추가", exact=True)).to_have_count(2)
            for code in ("005930", "000720", "003550"):
                expect(page.get_by_role("button", name=f"{code} 관심종목 추가", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="034220 관심종목 추가", exact=True)).to_have_count(1)
            assert page.evaluate("calls.every(([,method])=>method==='GET')")
            page.get_by_role("button", name="328130 관심종목 추가", exact=True).first.click()
            expect(page.get_by_text("관심종목에 추가됨", exact=True)).to_have_count(2)
            assert page.evaluate("calls.filter(([path,method])=>path==='/watchlist'&&method==='POST').map(([, ,body])=>body)") == [{"instrument_id": "owned-registered-id"}]
            page.evaluate("draw()")
            expect(page.get_by_role("button", name="034220 관심종목 추가", exact=True)).to_have_count(1)
            expect(page.get_by_role("button", name="328130 관심종목 추가", exact=True)).to_have_count(0)
            # A late registration lookup cannot write on behalf of a switched user/tenant.
            for tenant in ("one", "two"):
                page.evaluate("""() => {
                  state.user={id:'a',tenant_id:'one',role:'trader'};
                  document.querySelector('#actions').replaceChildren(watchlistAddControl({code:'999999',name:'다른종목'},Promise.resolve({owner:'a',tenant:'one',codes:new Set(),busy:new Set()})));
                }""")
                page.get_by_role("button", name="다른종목 관심종목 추가", exact=True).click()
                page.evaluate("tenant=>{state.user={id:'b',tenant_id:tenant,role:'trader'};deferredLookup({items:[{code:'999999',id:'other-id',status:'active'}]});}", tenant)
            assert page.evaluate("calls.filter(([path,method])=>path==='/watchlist'&&method==='POST').length") == 1
        assert page.evaluate("calls.every(([path])=>!path.includes('refresh')&&!path.includes('/trading')&&!path.includes('/initialize'))")
        browser.close()
