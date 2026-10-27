from types import SimpleNamespace

import pytest

from nastack_trading.trade_notifications import completion_message


@pytest.mark.parametrize(
    "broker,label", [("namuh", "나무증권 (NH투자증권)"), ("kiwoom", "영웅문 (키움증권)")]
)
def test_mobile_completion_format(broker, label):
    order = SimpleNamespace(
        status="filled",
        value={
            "name": "LG디스플레이",
            "code": "034220",
            "broker": broker,
            "side": "sell",
            "quantity": 1,
            "limit_price": 7170,
            "fill_price": 7180,
            "filled_quantity": 1,
            "remaining_quantity": 0,
        },
    )
    message = completion_message(order)
    assert f"\n증권사: {label}\n" in message
    assert "\n종목: LG디스플레이 (034220)\n" in message
    assert "주문 지정가: 7,170원\n체결수량: 1주\n평균 체결가: 7,180원\n미체결 잔량: 0주" in message
    assert "체결 아님" not in message
    order.status = "canceled"
    order.value.update(filled_quantity=0, fill_price=None, canceled_quantity=1)
    message = completion_message(order)
    assert "매도 주문 취소 완료" in message and "취소수량: 1주" in message
    assert "평균 체결가" not in message and "실현 수익률" not in message
    order.status = "accepted"
    with pytest.raises(ValueError):
        completion_message(order)


def test_completion_average_cost_gross_profit():
    order = SimpleNamespace(
        status="filled",
        value={
            "name": "Synthetic",
            "code": "034220",
            "broker": "kiwoom",
            "side": "sell",
            "fill_price": 105,
            "filled_quantity": 7,
            "remaining_quantity": 0,
            "sale_cost_basis": 100,
        },
    )
    message = completion_message(order)
    assert "실현 매매차익: +35원" in message
    assert "실현 수익률: +5.000%" in message
    assert "수수료·세금 제외" in message
