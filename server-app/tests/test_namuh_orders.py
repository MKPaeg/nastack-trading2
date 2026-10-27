from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from nastack_trading import namuh_orders
from nastack_trading.db import now
from nastack_trading.trading import order_view


def test_namuh_fill_and_cancel_wire_are_exact_and_fail_closed(monkeypatch):
    monkeypatch.setattr(namuh_orders, "order_account", lambda *args: "synthetic-account")
    row = {
        "itg_orr_no": 12,
        "iem_cd": "034220",
        "orr_qty": 1,
        "tot_cns_qty": 1,
        "ny_cns_qty": 0,
        "can_qty": 0,
        "cns_avg_uit_pr": 7170,
        "sby_dit_cd_nm": "매수",
        "orr_pr": 7180,
        "rmt_mkt_cd": "KRX",
        "sor_mkt_sli_yn": "N",
        "orr_rjt_rsn_cd_nm": "",
    }
    order = SimpleNamespace(
        created_at=now(),
        value={
            "broker_order_no": "12",
            "environment": "live",
            "code": "034220",
            "side": "buy",
            "quantity": 1,
            "limit_price": 7180,
            "cancel_quantity": 1,
        },
    )
    calls = []

    def request(*args):
        calls.append(args)
        return (
            {"Output_0": {"mkt_orr_no": 13}} if args[3].endswith("/cancel") else {"Output_0": [row]}
        ), "live"

    gateway = SimpleNamespace(request=request, validate_account=lambda *args: None)
    assert namuh_orders.fills(gateway, None, None, order) == {
        "filled_quantity": 1,
        "remaining_quantity": 0,
        "fill_price": 7170,
    }
    for key, value in [
        ("itg_orr_no", 14),
        ("iem_cd", "005930"),
        ("orr_qty", 2),
        ("sby_dit_cd_nm", "매도"),
        ("rmt_mkt_cd", "NXT"),
    ]:
        old = row[key]
        row[key] = value
        with pytest.raises(HTTPException):
            namuh_orders.fills(gateway, None, None, order)
        row[key] = old
    row.update(tot_cns_qty=0, ny_cns_qty=1, cns_avg_uit_pr=0)
    assert namuh_orders.fills(gateway, None, None, order)["remaining_quantity"] == 1
    assert namuh_orders.cancel(gateway, None, None, order) == "13"
    assert calls[-1][3] == "/krstock/order/v1/cancel"
    assert calls[-1][4] == {
        "act_no": "synthetic-account",
        "org_mkt_orr_no": 12,
        "iem_cd": "034220",
        "all_pat_dit_cd": "2",
        "cor_qty": 1,
    }
    row.update(ny_cns_qty=0, can_qty=1)
    assert namuh_orders.fills(gateway, None, None, order)["cancel_confirmed"] is True


def test_order_view_never_returns_account_ciphertext_or_key_fingerprint():
    order = SimpleNamespace(
        id="id",
        plan_id="plan",
        status="accepted",
        created_at=now(),
        value={"code": "034220", "account_ciphertext": "sensitive", "credential_scope": "sensitive"},
    )
    assert "sensitive" not in str(order_view(order, "Asia/Seoul"))


def test_namuh_requires_order_time_account_binding():
    gateway = SimpleNamespace(config=SimpleNamespace())
    # Missing scope must be rejected before decrypting or calling any external endpoint.
    # personal_config needs a DB; isolate it while exercising the binding guard.
    from unittest.mock import patch

    with (
        patch.object(namuh_orders, "personal_config", return_value=None),
        patch.object(namuh_orders, "NamuhAuth") as auth,
    ):
        auth.return_value.scope.return_value = "new-key-scope"
        with pytest.raises(HTTPException):
            namuh_orders.order_account(
                gateway,
                None,
                None,
                SimpleNamespace(value={"account_ciphertext": "old", "credential_scope": "old-scope"}),
            )
