import shutil
import subprocess
from pathlib import Path

import pytest


def test_stable_order_purchase_price_and_selected_row_scroll():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required")
    source = (Path(__file__).resolve().parents[1] / "trade-ui.js").read_text()
    helpers = source[source.index("function tradePurchaseLabel") : source.index("function setupPriceInput")]
    script = (
        helpers
        + """
const assert=require('node:assert/strict');
const priceText=n=>n.toLocaleString('ko-KR')+' 원';
assert.match(tradePurchaseLabel({quantity:3,purchase_price:12345}),/12,345 원/);
assert.match(tradePurchaseLabel({quantity:0,purchase_price:7190}),/보유 없음/);
assert.match(tradePurchaseLabel({quantity:1,purchase_price:null}),/확인 불가/);
assert.match(tradePurchaseLabel(null),/미조회/);
const a={id:'a',code:'034220',broker:'namuh',updated_at:'old'},b={id:'b',code:'046120',broker:'kiwoom',updated_at:'new'};
assert.deepEqual(stableTradePlans([b,a]).map(p=>p.id),['a','b']);
assert.deepEqual(stableTradePlans([{...a,updated_at:'newest'},b]).map(p=>p.id),['a','b']);
const scroller={scrollTop:300,scrollHeight:2000,clientHeight:600,parentElement:null,getBoundingClientRect:()=>({top:76})};
let base=400,highlight=false;
const row={dataset:{tradeRow:'a'},parentElement:scroller,getBoundingClientRect:()=>({top:base-scroller.scrollTop,bottom:base-scroller.scrollTop+800}),classList:{add:()=>highlight=true}};
const wrap={scrollLeft:120};
const section={isConnected:true,querySelectorAll:()=>[row],querySelector:()=>wrap};
const getComputedStyle=()=>({overflowY:'auto'});
const anchor=captureTradeViewport(section,'a');
assert.equal(anchor.top,100);
base=600;wrap.scrollLeft=0;
restoreTradeViewport(section,anchor);
assert.equal(row.getBoundingClientRect().top,84);
assert.equal(wrap.scrollLeft,120);assert.equal(highlight,true);
const refreshAnchor=captureTradeViewport(section);
assert.equal(refreshAnchor.alignTop,false);
base+=70;restoreTradeViewport(section,refreshAnchor);
assert.equal(row.getBoundingClientRect().top,84);
const before=scroller.scrollTop;section.isConnected=false;
restoreTradeViewport(section,anchor);assert.equal(scroller.scrollTop,before);
"""
    )
    subprocess.run([executable, "-e", script], check=True, capture_output=True, text=True)
    assert "await load(anchor)" in source
    assert "purchase.textContent=tradePurchaseLabel(data.position)" in source
