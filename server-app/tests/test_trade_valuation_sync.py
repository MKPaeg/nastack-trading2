import shutil
import subprocess
from pathlib import Path

import pytest


def test_mark_to_market_follows_same_quote_not_realized_profit():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required")
    source = (Path(__file__).resolve().parents[1] / "trade-ui.js").read_text()
    helper = source[source.index("function tradeMarkToMarket") : source.index("function tradePurchaseLabel")]
    script = (
        helper
        + """
const assert=require('node:assert/strict');
const position={quantity:10,purchase_price:100};
assert.deepEqual(tradeMarkToMarket(position,{price:105}),{profit:50,rate:(105/100-1)*100});
assert.equal(tradeMarkToMarket(position,{price:95}).profit,-50);
assert.equal(tradeMarkToMarket(position,{price:100}).profit,0);
assert.deepEqual(tradeMarkToMarket({quantity:0},{price:100}),{profit:0,rate:null});
assert.equal(tradeMarkToMarket(null,{price:100}),null);
assert.equal(tradeMarkToMarket({quantity:1,purchase_price:null},{price:100}),null);
assert.equal(tradeMarkToMarket(position,{price:NaN}),null);
assert.equal(tradeMarkToMarket(position,{price:0}),null);
assert.equal(tradeMarkToMarket({...position,quantity:-1},{price:100}),null);
"""
    )
    subprocess.run([executable, "-e", script], check=True, capture_output=True, text=True)
    assert "performance.setQuote({...snapshot,price:current??last?.close})" in source
    assert "received<lastQuoteTime" in source
    assert "오늘 앱 실현손익" in source
    assert "현재가 기준" in source
    assert "valuationNote.title=quote?" in source
