import shutil
import subprocess
from pathlib import Path

import pytest


def test_average_cost_return_and_automatic_holdings_contract():
    source = (Path(__file__).resolve().parents[1] / "app.js").read_text()
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required")
    helper = (
        "function holdingPurchaseReturn"
        + source.split("function holdingPurchaseReturn", 1)[1].split(
            "function paintHoldingPurchaseReturn", 1
        )[0]
    )
    script = (
        helper
        + """
const assert=require('node:assert/strict');
const item={quantity:5,purchase_price:100};
assert(Math.abs(holdingPurchaseReturn(item,110)-10)<1e-8);
assert(Math.abs(holdingPurchaseReturn(item,90)+10)<1e-8);
for(const purchase_price of [null,0,-1,NaN])assert.equal(holdingPurchaseReturn({...item,purchase_price},100),null);
assert.equal(holdingPurchaseReturn({...item,quantity:0},100),null);
assert.equal(holdingPurchaseReturn({...item,quantity:null},100),null);
assert.equal(holdingPurchaseReturn(item,null),null);
"""
    )
    subprocess.run([executable, "-e", script], check=True, capture_output=True, text=True)
    holdings = source.split("async function renderHoldings()", 1)[1].split(
        "async function renderPeriodReturns", 1
    )[0]
    assert "refresh.remove()" in holdings
    assert "await autoRefresh();" in holdings
    assert "},60000)" in holdings
    assert "state.user?.id===owner" in holdings
    assert "document.hidden||refreshing" in holdings
    assert 'state.user.role==="viewer"' in holdings
    assert "matching.length===1" in holdings
    assert "if(code&&!isKiwoom)bindLivePrice" in holdings
    assert "paintHoldingPurchaseReturn(returns,item,quote.price)" in holdings
    assert "/trading/" not in holdings
