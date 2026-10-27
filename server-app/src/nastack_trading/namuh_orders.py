"""NH read-only order ledger and explicitly requested residual cancellation."""

from fastapi import HTTPException

from .brokers import NamuhAuth
from .db import local_time
from .market import numeric
from .user_brokers import personal_config


def order_account(gateway, db, user, order):
    auth = NamuhAuth(personal_config(db, user, gateway.config, "namuh"))
    value = order.value
    if not value.get("account_ciphertext") or value.get("credential_scope") != auth.scope():
        raise HTTPException(409, "주문 당시 계좌·인증 설정 확인 필요 · 현재 설정으로 대체하지 않습니다.")
    try:
        return auth.cipher().decrypt(value["account_ciphertext"].encode()).decode()
    except Exception:
        raise HTTPException(409, "주문 계좌 복호화 실패 · 서버 키를 확인하세요.") from None


def fills(gateway, db, user, order):
    v = order.value
    account = order_account(gateway, db, user, order)
    raw, environment = gateway.request(
        db,
        user,
        "namuh",
        "/krstock/inquiry/v1/dailyOrderExecution",
        {
            "act_no": account,
            "orr_dt": local_time(order.created_at, "Asia/Seoul")[:10].replace("-", ""),
            "orr_mkt_cd": "00",
            "ost_cns_dit": "0",
        },
    )
    if environment != v["environment"] or raw.get("_has_more"):
        raise HTTPException(409, "환경 또는 연속조회 확인 필요 · 확정 보류")
    # Never pick by stock/quantity alone. If the returned integrated number cannot
    # be matched to the submitted order identifier, require provider reconciliation.
    rows = [
        r
        for r in raw.get("Output_0", [])
        if isinstance(r, dict)
        and str(r.get("itg_orr_no", "")).lstrip("0") == str(v["broker_order_no"]).lstrip("0")
        and str(r.get("iem_cd", "")) == v["code"]
    ]
    if len(rows) != 1:
        raise HTTPException(409, "나무 주문번호의 정확한 원장 대조 필요 · 재주문하지 마세요.")
    r = rows[0]
    qty, filled, remaining, canceled, price = (
        numeric(r.get(k)) for k in ("orr_qty", "tot_cns_qty", "ny_cns_qty", "can_qty", "cns_avg_uit_pr")
    )
    if (
        any(n is None or n < 0 or n != int(n) for n in (qty, filled, remaining, canceled))
        or qty != v["quantity"]
        or filled + remaining + canceled != qty
        or ("매수" if v["side"] == "buy" else "매도") not in str(r.get("sby_dit_cd_nm", ""))
        or (filled and (price is None or price <= 0))
        or r.get("rmt_mkt_cd") != "KRX"
        or r.get("sor_mkt_sli_yn") != "N"
        or str(r.get("orr_rjt_rsn_cd_nm", "")).strip() not in {"", "정상"}
        or (v.get("limit_price") is not None and numeric(r.get("orr_pr")) != v["limit_price"])
    ):
        raise HTTPException(409, "나무 주문 수량·방향·시장·거부 여부 확인 필요 · 확정 보류")
    result = {
        "filled_quantity": int(filled),
        "remaining_quantity": int(remaining),
        "fill_price": price if filled else None,
    }
    if canceled:
        result.update(canceled_quantity=int(canceled), cancel_confirmed=remaining == 0)
    return result


def cancel(gateway, db, user, order):
    v = order.value
    account = order_account(gateway, db, user, order)
    gateway.validate_account(db, user, "namuh", account)
    data, environment = gateway.request(
        db,
        user,
        "namuh",
        "/krstock/order/v1/cancel",
        {
            "act_no": account,
            "org_mkt_orr_no": int(v["broker_order_no"]),
            "iem_cd": v["code"],
            "all_pat_dit_cd": "2",
            "cor_qty": v["cancel_quantity"],
        },
    )
    number = data.get("Output_0", {}).get("mkt_orr_no")
    if environment != v["environment"] or not number:
        raise HTTPException(502, "나무 취소 응답 확인 필요 · 재전송 금지")
    return str(number)
