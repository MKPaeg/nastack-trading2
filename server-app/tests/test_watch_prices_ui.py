"""Watchlist pages read DB snapshots and never request provider refresh."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_watch_refresh_flags_match_server_contract():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for frontend contract verification")
    source = (Path(__file__).resolve().parents[1] / "app.js").read_text()
    function = (
        "async function updateWatchPrices(wrap){"
        + source.split("async function updateWatchPrices(wrap){", 1)[1].split("function threeMonthPeak", 1)[0]
    )
    script = (
        """
const assert=require('node:assert/strict');
const state={user:{id:'a',role:'trader'},view:'watchlist'},document={hidden:false};
const setTimeout=()=>0,clearTimeout=()=>{},node=(tag,text)=>({tag,text,dataset:{}});
const priceText=x=>String(x),formatTime=x=>x,movementText=x=>String(x),movementClass=()=>'',threeMonthPeak=()=>null,seoulDate=()=>'';
const cells=['034220','046120'].map(code=>({dataset:{watchPrice:code},parentElement:{querySelector:()=>null},querySelector:()=>null,replaceChildren(...values){this.values=values},append(){}}));
const wrap={isConnected:true,querySelectorAll:()=>cells};let calls=[];
async function api(path,options){calls.push([path,JSON.parse(options.body)]);return {is_open:true,items:Object.fromEntries(cells.map(c=>[c.dataset.watchPrice,{snapshot:{price:100,collected_at:'now'}}]))};}
"""
        + function
        + """
(async()=>{
await updateWatchPrices(wrap);assert.equal(calls.length,1);
assert.equal(calls[0][0],'/watchlist/quotes');assert.deepEqual(calls[0][1].codes,['034220','046120']);
assert.equal(calls[0][1].refresh,false);
assert(cells.every(c=>c.values[0].text==='100'));
await updateWatchPrices(wrap);assert.equal(calls.length,1); // Due-time dedup.
document.hidden=true;wrap._quotesDue=0;await updateWatchPrices(wrap);assert.equal(calls.length,1);
})().catch(e=>{console.error(e);process.exit(1)});
"""
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    assert "updateWatchPricesLegacy" not in source
