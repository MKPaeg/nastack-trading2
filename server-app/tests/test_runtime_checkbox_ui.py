import shutil
import subprocess
from pathlib import Path

import pytest


def test_runtime_checkbox_direction_permissions_restore_and_layout():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required")
    source = (Path(__file__).resolve().parents[1] / "trade-ui.js").read_text()
    helper = "async function renderAutomaticRuntime" + source.split(
        "async function renderAutomaticRuntime", 1
    )[1].split("async function renderTradeNotificationChannels", 1)[0]
    script = r"""
const assert=require('node:assert/strict');
class E{constructor(tag,text,cls){this.tag=tag;this.textContent=text;this.className=cls;this.children=[];this.isConnected=true;this.events={};}append(...items){this.children.push(...items)}prepend(e){this.children.unshift(e)}setAttribute(){}addEventListener(type,cb){this.events[type]=cb}}
const node=(...args)=>new E(...args);
let state={user:{id:'a',role:'trader'}},saved={version:0,paused:false},calls=[],confirmResult=true;
const window={confirm:()=>confirmResult};
async function api(path,options){if(options){const payload=JSON.parse(options.body);calls.push(payload);saved={version:payload.version+1,paused:payload.paused};}return {...saved};}
""" + helper + r"""
(async()=>{
 const section=node('section');await renderAutomaticRuntime(section);
 const box=section.children[0],label=box.children[0],input=label.children[0];
 assert.equal(input.type,'checkbox');assert.equal(input.checked,true);
 assert.equal(box.tag,'div');assert.equal(box.children[1].tag,'p');assert.equal(box.children[2].className,'helper');
 assert.equal(box.children.filter(e=>e.tag==='button').length,0);
 input.checked=false;await input.events.change();assert.equal(calls.at(-1).paused,true);assert.equal(input.checked,false);
 confirmResult=false;input.checked=true;await input.events.change();assert.equal(input.checked,false);assert.equal(calls.length,1);
 confirmResult=true;input.checked=true;await input.events.change();assert.equal(calls.at(-1).paused,false);
 const restored=node('section');await renderAutomaticRuntime(restored);assert.equal(restored.children[0].children[0].children[0].checked,true);
 state.user={id:'b',role:'viewer'};const readonly=node('section');await renderAutomaticRuntime(readonly);const r=readonly.children[0].children[0].children[0];assert.equal(r.disabled,true);await r.events.change();assert.equal(calls.length,2);
 state.user={id:'other',role:'trader'};await input.events.change();assert.equal(calls.length,2);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    subprocess.run([executable, "-e", script], check=True, capture_output=True, text=True)
    assert 'await renderAutomaticRuntime(safetyBox)' in source
    assert 'safetyBox.append(safetyNote,explanations,runtimeBox)' in source
