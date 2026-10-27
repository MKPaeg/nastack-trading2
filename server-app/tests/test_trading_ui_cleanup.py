from pathlib import Path


def test_normal_trading_screen_does_not_start_paper_ui_or_polling():
    root = Path(__file__).resolve().parents[1]
    source = (root / "trade-ui.js").read_text()
    trading = source.split("async function renderTrading()", 1)[1].split(
        "async function renderTradingSettings", 1
    )[0]
    assert "await renderPaperTrading(" not in trading
    assert 'api("/trading/paper' not in trading
    assert "function renderPaperTrading(" in source  # Legacy test implementation preserved.
    help_source = (root / "screen-help.js").read_text()
    assert "Paper 시뮬레이션" not in help_source
    assert 'profiles: ["paper-test"]' in (root / "compose.deploy.yaml").read_text()
    assert "테스트 매수가격 (원)" not in trading
    assert "테스트 매도가격 (원)" not in trading
    assert 'node("button","소액 지정가' not in trading
    assert "알림 채널 변경 · 내 설정" not in trading
    assert 'message.textContent=kiwi?' not in trading
    assert 'const tradeTime=' not in trading
    controls = (root / "auto-trade-ui.js").read_text()
    assert 'node("button","자동매매 시작"' in controls
    assert 'node("button","예약"' in controls
    assert '예약 시간 · 한국시간' in controls
    assert 'confirmation:"실전 자동매매 시작 확인"' in controls
    assert 'monitorTradingWorker(section,metadata)' in trading


def test_trade_day_change_and_entry_only_sidebar():
    import shutil
    import subprocess

    import pytest

    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node.js required for frontend checks")
    root = Path(__file__).resolve().parents[1]
    source = (root / "trade-ui.js").read_text()
    helper = next(line for line in source.splitlines() if line.startswith("function tradeDayChange("))
    app = (root / "app.js").read_text()
    open_view = next(line for line in app.splitlines() if line.startswith("function openView("))
    script = (
        helper
        + "\n"
        + open_view
        + """
const assert=require('node:assert/strict');
assert.deepEqual(tradeDayChange({price:110,reference_close:100}),{amount:10,rate:10,reference:100});
assert.equal(tradeDayChange({price:90,reference_close:100}).rate,-10);
assert.equal(tradeDayChange({price:100,reference_close:100}).amount,0);
assert.equal(tradeDayChange({price:100}),null);
assert.equal(tradeDayChange({price:100,reference_close:0}),null);
assert.equal(tradeDayChange({price:NaN,reference_close:100}),null);
let collapsed=0;
const state={view:'dashboard'},stopStockPolling=()=>{},reportError=e=>{throw e;};
const $=()=>({classList:{add:()=>collapsed++}});
const document={querySelectorAll:()=>[]},render=()=>Promise.resolve();
openView('orders');assert.equal(collapsed,1);
openView('orders');assert.equal(collapsed,1);
openView('analysis');assert.equal(collapsed,1);
openView('orders');assert.equal(collapsed,2);
"""
    )
    subprocess.run([executable, "-e", script], check=True, capture_output=True, text=True)


def test_manual_optional_limit_price():
    import shutil
    import subprocess

    import pytest
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required")
    source = (Path(__file__).resolve().parents[1] / "trade-ui.js").read_text()
    helper = next(line for line in source.splitlines() if line.startswith("function manualLimitPrice("))
    formatter = next(line for line in source.splitlines() if line.startswith("function formatPriceInput("))
    script = helper + "\n" + formatter + """
const assert=require('node:assert/strict');
assert.equal(manualLimitPrice(''),null);
assert.equal(manualLimitPrice('  '),null);
assert.equal(manualLimitPrice('7190'),7190);
assert.equal(manualLimitPrice('7,190'),7190);
assert.equal(manualLimitPrice('1,000,000'),1000000);
assert.equal(formatPriceInput('7190'),'7,190');
assert.equal(formatPriceInput('1000000'),'1,000,000');
assert.equal(formatPriceInput('7,190'),'7,190');
assert.equal(formatPriceInput(''),'');
assert.equal(formatPriceInput('1.5'),'1.5');
assert.throws(()=>manualLimitPrice('1,23'));
for(const value of ['0','-1','1.5','abc','Infinity','1000000001'])assert.throws(()=>manualLimitPrice(value));
"""
    subprocess.run([executable, "-e", script], check=True, capture_output=True, text=True)
    assert 'adjustPosition(plan,side,autoMax,button,limit)' in source
    assert '"매수 최대가격 필수"' in source
    assert 'sellPrice.placeholder="비우면 시장가"' in source
    assert 'purpose:"position_adjustment"' in source
