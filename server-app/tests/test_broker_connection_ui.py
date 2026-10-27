"""Exercise the integrated personal-key flow without broker requests or real credentials."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_personal_connection_flow_uses_saved_keys_and_never_orders():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    source = (Path(__file__).resolve().parents[1] / "trade-ui.js").read_text()
    source = source.split("async function renderPersonalBrokerKeys", 1)[1].split("let screenStateSignature", 1)[0]
    harness = """
const assert=require('node:assert/strict');
const state={user:{id:'owner',role:'trader'}};
function node(tag,text){return {tag,textContent:text||'',children:[],value:'',isConnected:true,classList:{add(){}},handlers:{},setAttribute(){},append(...x){this.children.push(...x)},addEventListener(n,f){this.handlers[n]=f}};}
const panel=t=>node('section',t),formatTime=x=>x;
let calls=[],enabled=false,fail=false;
async function api(path,options){calls.push([path,options]);
 if(path.endsWith('/keys'))return {configured:true,version:1};
 if(path.endsWith('/verify'))return {enabled:true,last_probe:{status:fail?'failed':'completed',message:'verification failed'},token_expires_at:'tomorrow'};
 if(path==='/portfolio/accounts')return {accounts:[{},{}]};
 if(options?.method==='PUT')enabled=true;
 return {enabled,version:4,environment:'live'};
}
"""
    checks = """
(async()=>{
 const area=node('div');await renderPersonalBrokerKeys(area,'namuh','Namuh');
 const section=area.children[0],buttons=section.children.find(x=>x.tag==='div'&&x.children.some(c=>c.tag==='button'));
 await buttons.children[1].handlers.click();
 assert(!calls.some(([p,o])=>p.endsWith('/keys')&&o));
 assert(calls.some(([p,o])=>p==='/user/brokers/namuh'&&o?.method==='PUT'));
 assert(calls.some(([p])=>p==='/portfolio/accounts'));
 assert(section.children.at(-1).textContent.includes('계좌 조회 성공 (2개)'));
 fail=true;await buttons.children[1].handlers.click();
 assert(section.children.at(-1).textContent.includes('인증 단계 실패'));
 assert(calls.every(([p])=>!p.includes('/trading/')&&!p.includes('/orders')));
 state.user.id='other';const count=calls.length;await buttons.children[1].handlers.click();assert.equal(calls.length,count);
})().catch(e=>{console.error(e);process.exit(1)});
"""
    subprocess.run([node, "-e", harness + "async function renderPersonalBrokerKeys" + source + checks],
                   check=True, capture_output=True, text=True)
