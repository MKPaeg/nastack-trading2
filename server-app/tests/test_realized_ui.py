from pathlib import Path


def test_realized_tab_has_periods_owner_guard_and_read_only_report():
    source = (Path(__file__).resolve().parents[1] / "trade-ui.js").read_text()
    assert '["realized","실현손익"]' in source
    report = source.split("async function renderRealizedSales", 1)[1].split("async function renderTradingSettings", 1)[0]
    for period in ["day", "week", "month", "year"]:
        assert f'["{period}",' in report
    assert 'state.user?.id===owner' in report
    assert '/trading/realized?' in report
    assert '/trading/realized/sync' in report
    assert '/orders/' not in report
    assert 'state.user.role==="viewer"' in report
    assert '과거 체결 반영' in report
    assert 'orderRealizedText(order)' in source
