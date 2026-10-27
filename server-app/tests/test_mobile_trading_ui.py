import shutil
import subprocess
from pathlib import Path

import pytest


def test_mobile_cards_keep_existing_controls_and_no_order_calls():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required")
    root = Path(__file__).resolve().parents[1]
    source = (root / "trade-ui.js").read_text()
    helper = (
        "function prepareMobileTradeCard"
        + source.split("function prepareMobileTradeCard", 1)[1].split("async function renderPaperTrading", 1)[
            0
        ]
    )
    harness = (
        """
const assert=require('node:assert/strict');
class E{constructor(tag,text){this.tag=tag;this.textContent=text;this.children=[];this.dataset={};}get firstChild(){return this.children[0]}append(e){if(e.parent){e.parent.children.splice(e.parent.children.indexOf(e),1)}this.children.push(e);e.parent=this;}}
const node=(tag,text,cls)=>{const e=new E(tag,text);e.className=cls;return e;};
const media={matches:true,addEventListener(type,cb){this.change=cb}};
const window={matchMedia:()=>media};
const document={querySelectorAll:()=>[2,3,4].map(i=>row.children[i].firstChild)};
const row=new E('tr');for(let i=0;i<6;i++){const td=new E('td');td.append(new E('input'));row.append(td);}
const original=row.children[4].firstChild;original.disabled=true;
"""
        + helper
        + """
prepareMobileTradeCard(row);
for(const i of [2,3,4]){assert.equal(row.children[i].firstChild.open,false);assert.equal(row.children[i].firstChild.firstChild.tag,'summary');}
assert.equal(row.children[4].firstChild.children[1],original);assert.equal(original.disabled,true);
assert.equal(row.children[0].firstChild.tag,'input');assert.equal(row.children[1].firstChild.tag,'input');assert.equal(row.children[5].firstChild.tag,'input');
media.matches=false;media.change();for(const i of [2,3,4])assert.equal(row.children[i].firstChild.open,true);
media.matches=true;media.change();for(const i of [2,3,4])assert.equal(row.children[i].firstChild.open,false);
"""
    )
    subprocess.run([executable, "-e", harness], check=True, capture_output=True, text=True)
    assert "api(" not in helper
    css = (root / "mobile.css").read_text()
    assert "max-width:800px" in css
    assert "min-width:0!important" in css
    assert "min-height:44px" in css
    assert ".vector-controls>label{min-width:0;max-width:100%}" in css
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in css
    assert "::-webkit-date-and-time-value" in css
    assert '"mobile.css"' in (root / "src/nastack_trading/app.py").read_text()
