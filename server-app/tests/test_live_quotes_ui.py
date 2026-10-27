"""Run the quote queue with mocked DOM/API: no broker connections or orders."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_quote_queue_visibility_owner_dedup_and_preserves_price():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required")
    source = (Path(__file__).resolve().parents[1] / "live-quotes.js").read_text()
    harness = """
const assert=require('node:assert/strict');
let clock=100000; Date.now=()=>clock;
const state={user:{id:'a',tenant_id:'one',role:'user'}};
const document={hidden:false,addEventListener(){},createElement(){return {dataset:{}};}};
const setInterval=()=>0;
 let calls=[],fail=false,open=true,cadence=0;
async function api(path,options){calls.push([path,options]);if(path==='/market/session')return {is_open:open};if(fail)throw Error('limited');return {snapshot:{price:100,collected_at:'now',refresh_after_ms:cadence}};}
const cell=()=>({isConnected:true,children:[],append(x){this.children.push(x)},querySelector(){return null}});
"""
    checks = """
(async()=>{
 const a=cell(),b=cell();let prices=[];
 liveQuotes.subscribe(a,'005930',s=>prices.push(s.price));
 liveQuotes.subscribe(b,'005930',s=>prices.push(s.price));
 await liveQuotes.tick();assert.deepEqual(prices,[100,100]);
 assert.equal(calls.filter(c=>c[0].includes('/refresh')).length,1);
 assert(calls[1][0].endsWith('?auto=true&quote_only=true'));
 clock+=6000;document.hidden=true;await liveQuotes.tick();assert.equal(calls.length,2);
 document.hidden=false;fail=true;await liveQuotes.tick();assert.deepEqual(prices,[100,100,100,100]);assert.equal(a.children.length,1);assert(a.children[0].textContent.includes('limited'));
 clock+=31000;state.user={id:'b',tenant_id:'two',role:'user'};await liveQuotes.tick();assert.equal(calls.length,3);
 const c=cell();liveQuotes.subscribe(c,'000720',s=>prices.push(s.price));fail=false;open=false;clock+=31000;await liveQuotes.tick();assert(!calls.at(-1)[0].includes('/refresh'));
 c.isConnected=false;clock+=61000;const count=calls.length;await liveQuotes.tick();assert.equal(calls.length,count);
 // Viewer can read stored data only, even during the trading session.
 state.user={id:'v',tenant_id:'one',role:'viewer'};open=true;clock+=61000;
 liveQuotes.subscribe(cell(),'005930',()=>{});await liveQuotes.tick();assert(!calls.at(-1)[0].includes('/refresh'));
 assert(calls.every(c=>!c[0].includes('/trading/')&&!c[0].includes('/news/')));
 state.user={id:'trader',tenant_id:'two',role:'trader'};clock+=61000;open=true;
 const first=[],second=[];
 liveQuotes.subscribe(cell(),'046120',s=>first.push(s.price),'plan-a');
 liveQuotes.subscribe(cell(),'046120',s=>second.push(s.price),'plan-b');
 await liveQuotes.tick();assert.deepEqual(first,[100]);assert.deepEqual(second,[100]);
 assert(calls.some(c=>c[0]==='/trading/plans/plan-a/quote'));
 assert(calls.at(-1)[0]==='/trading/plans/plan-b/quote');
 // Both trading plans become due again after one second, not two seconds
 // added after provider response latency. Public lane remains independent.
 const before=calls.filter(c=>c[0].includes('/trading/')).length;
 clock+=1000;await liveQuotes.tick();
 assert.equal(calls.filter(c=>c[0].includes('/trading/')).length,before+2);
 // NH can advertise a slower display cadence; Kiwoom/default remains one second.
 cadence=10000;clock+=1000;await liveQuotes.tick();
 const slow=calls.filter(c=>c[0].includes('/trading/')).length;
 clock+=1000;await liveQuotes.tick();assert.equal(calls.filter(c=>c[0].includes('/trading/')).length,slow);
 clock+=9000;await liveQuotes.tick();assert.equal(calls.filter(c=>c[0].includes('/trading/')).length,slow+2);
})().catch(e=>{console.error(e);process.exit(1)});
"""
    subprocess.run([executable, "-e", harness + source + checks], check=True, capture_output=True, text=True)


def test_quote_script_served_and_all_list_views_subscribe():
    root = Path(__file__).resolve().parents[1]
    assert '"live-quotes.js"' in (root / "src/nastack_trading/app.py").read_text()
    assert '/live-quotes.js?' in (root / "index.html").read_text()
    app = (root / "app.js").read_text()
    assert 'bindLivePrice(tr.children[2],code,' in app
    assert 'api("/watchlist/quotes"' in app
    assert 'bindLivePrice(price,plan.code,paintQuote,plan.id)' in (root / "trade-ui.js").read_text()
