from nastack_trading.market import holding_quantity


def test_residual_tracks_executed_position_not_settlement_activity():
    assert holding_quantity({"itg_bnc_qty": 0, "ny_stl_qty": 6, "rsdl_qty": 5}) == 5
    assert holding_quantity({"itg_bnc_qty": 0, "ny_stl_qty": 1, "rsdl_qty": 0}) == 0
    assert holding_quantity({"itg_bnc_qty": 130, "ny_stl_qty": 130, "rsdl_qty": 130}) == 130
    assert holding_quantity({"itg_bnc_qty": 130, "ny_stl_qty": -30, "rsdl_qty": 100}) == 100
    assert holding_quantity({"itg_bnc_qty": 130, "rsdl_qty": 0}) == 0
    assert holding_quantity({"itg_bnc_qty": 130, "ny_stl_qty": 0}) is None
    assert holding_quantity({"rsdl_qty": "10"}) == 10
    assert holding_quantity({}) is None
    assert holding_quantity({"ny_stl_qty": 1, "rsdl_qty": 1}) == 1
    for bad in [-1, "NaN", "Infinity", "invalid", 1.5]:
        assert holding_quantity({"rsdl_qty": bad}) is None
