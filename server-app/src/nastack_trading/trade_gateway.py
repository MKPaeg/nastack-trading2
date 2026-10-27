"""Explicit cash-only KRX order gateway. Never invoked by quote readers."""

import math
from datetime import timedelta

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from .brokers import API_URLS, NamuhAuth
from .db import aware, now
from .kiwoom import HOSTS, KiwoomReader
from .market import eligible_account, holding_quantity, numeric
from .models import BrokerConnection, BrokerSnapshot
from .namuh_reads import critical_reads


class TradeGateway:
    def __init__(self, config, namuh_transport=None, kiwoom_transport=None):
        self.config = config
        self.transports = {"namuh": namuh_transport, "kiwoom": kiwoom_transport}

    def credentials(self, db, user, broker):
        from .user_brokers import personal_config

        config = personal_config(db, user, self.config, broker)
        if broker == "kiwoom":
            token, environment = KiwoomReader(self.config, self.transports[broker]).token(db, user)
            return environment, {"authorization": f"Bearer {token}"}
        if user.email.lower() != config.namuh_owner_email.lower():
            raise HTTPException(403, "나무증권 연결 소유자만 주문할 수 있습니다.")
        auth = NamuhAuth(config)
        row = db.scalar(
            select(BrokerConnection).where(
                BrokerConnection.owner_id == user.id, BrokerConnection.tenant_id == user.tenant_id
            )
        )
        if (
            not row
            or not row.enabled
            or not row.token_expires_at
            or aware(row.token_expires_at) <= now() + timedelta(seconds=60)
            or row.token_scope != auth.scope()
        ):
            raise HTTPException(409, "나무증권 인증을 관리자 설정에서 확인하세요.")
        token = auth.cipher().decrypt(row.token_ciphertext.encode()).decode()
        return row.environment, {
            "authorization": f"Bearer {token}",
            "x-client-id": config.namuh_app_key.get_secret_value(),
            "x-client-secret": config.namuh_app_secret.get_secret_value(),
        }

    def request(self, db, user, broker, path, body, api_id=None):
        if broker != "namuh" or user is None:
            return self._request(db, user, broker, path, body, api_id)
        from .namuh_reads import read_gate
        from .user_brokers import personal_config

        environment, headers = self.credentials(db, user, broker)
        gate = read_gate(user, environment, NamuhAuth(personal_config(db, user, self.config, broker)).scope())
        ordering = "/order/" in path
        reconciling = path.endswith("dailyOrderExecution")
        if ordering or reconciling:
            gate.invalidate()
            try:
                return self._request(db, user, broker, path, body, api_id)
            finally:
                gate.invalidate()  # Also invalidate on ambiguous replies; never retry them.
        return gate.read(
            path,
            body,
            lambda: self._request(db, user, broker, path, body, api_id),
            headers.get("authorization", ""),
        )

    def _request(self, db, user, broker, path, body, api_id=None):
        environment, headers = self.credentials(db, user, broker)
        allowed_namuh = {
            "/n2/acctinfo",
            "/krstock/inquiry/v1/buyableQuantity",
            "/krstock/inquiry/v1/sellableQuantity",
            "/krstock/inquiry/v1/balance",
            "/krstock/quote/v1/currentPrice",
            "/krstock/quote/v1/currentDaily",
            "/krstock/order/v1/cashBuy",
            "/krstock/order/v1/cashSell",
            "/krstock/order/v1/cancel",
            "/krstock/inquiry/v1/dailyOrderExecution",
        }
        if broker == "namuh" and path not in allowed_namuh:
            raise HTTPException(403, "허용되지 않은 매매 경로입니다.")
        if broker == "kiwoom" and (path, api_id) not in {
            ("/api/dostk/acnt", "kt00011"),
            ("/api/dostk/acnt", "kt00018"),
            ("/api/dostk/acnt", "kt00007"),
            ("/api/dostk/stkinfo", "ka10001"),
            ("/api/dostk/ordr", "kt10000"),
            ("/api/dostk/ordr", "kt10001"),
            ("/api/dostk/ordr", "kt10003"),
        }:
            raise HTTPException(403, "허용되지 않은 매매 TR입니다.")
        ordering = "/order/" in path or path == "/api/dostk/ordr"
        gate = None
        if broker == "namuh" and not ordering and user is not None:
            from .namuh_reads import read_gate
            from .user_brokers import personal_config

            gate = read_gate(
                user, environment, NamuhAuth(personal_config(db, user, self.config, broker)).scope()
            )
            gate.acquire()
        if ordering and not self.config.live_orders_enabled:
            raise HTTPException(409, "서버 실전 주문 안전잠금이 켜져 있습니다.")
        if api_id:
            headers["api-id"] = api_id
        try:
            with httpx.Client(
                timeout=15, trust_env=False, follow_redirects=False, transport=self.transports[broker]
            ) as client:
                if ordering:
                    from .order_boundary import before_transmission
                    from .trade_safety import ensure_new_order_allowed

                    # Credentials may refresh/commit: authorize AFTER that work, not before.
                    if db is not None and not path.endswith(("/cancel", "/ordr")):
                        ensure_new_order_allowed(db, user, self.config)
                    if api_id in {"kt10000", "kt10001"} and db is not None:
                        ensure_new_order_allowed(db, user, self.config)
                    before_transmission()
                response = client.post(
                    (API_URLS if broker == "namuh" else HOSTS)[environment] + path,
                    json={"Input_0": body} if broker == "namuh" else body,
                    headers=headers,
                )
            if response.status_code == 429 and gate is not None:
                gate.limited(response.headers.get("Retry-After"))
            if response.status_code != 200 or len(response.content) > 2_000_000:
                raise HTTPException(502, "증권사 응답 확인 필요. 주문은 자동 재전송하지 않습니다.")
            data = response.json()
            if not isinstance(data, dict) or (broker == "kiwoom" and str(data.get("return_code")) != "0"):
                raise HTTPException(502, "증권사가 정상 응답하지 않았습니다. 주문 이력을 확인하세요.")
            if api_id in {"kt00007", "kt00018"}:
                data["_has_more"] = response.headers.get("cont-yn") == "Y"
            if broker == "namuh":
                message = data.get("message")
                code = data.get("rsp_cd") or (message.get("rsp_cd", "") if isinstance(message, dict) else "")
                if code == "IGW42902" and gate is not None:
                    gate.limited(response.headers.get("Retry-After"))
                if str(code).startswith("IGW"):
                    raise HTTPException(502, "나무증권 업무 오류 · 원장 확인 필요 · 재전송 금지")
                if path.endswith(("dailyOrderExecution", "/balance")):
                    data["_has_more"] = response.headers.get("cont-yn") == "Y" or bool(data.get("cts"))
            return data, environment
        except (httpx.HTTPError, ValueError, TypeError):
            raise HTTPException(502, "증권사 응답 불명. 재주문 전 원장 확인이 필요합니다.") from None

    def validate_account(self, db, user, broker, account):
        if broker == "kiwoom":
            self.credentials(db, user, broker)
            return
        data, environment = self.request(db, user, broker, "/n2/acctinfo", {})
        if account not in {
            str(r.get("acct_no")) for r in data.get("Output_0", []) if eligible_account(r, environment)
        }:
            raise HTTPException(422, "현재 연결 환경에 속한 계좌를 선택하세요.")

    def fills(self, db, user, order):
        """Read-only exact order reconciliation; never infers a fill from a balance."""
        from .db import local_time

        value = order.value
        if value["broker"] == "namuh":
            from .namuh_orders import fills

            return fills(self, db, user, order)
        if value["broker"] != "kiwoom":
            raise HTTPException(409, "이 증권사의 체결 조회는 아직 연결되지 않았습니다.")
        if value.get("credential_scope"):
            from .user_brokers import personal_config

            scope = KiwoomReader(personal_config(db, user, self.config, "kiwoom")).scope(value["environment"])
            if scope != value["credential_scope"]:
                raise HTTPException(
                    409, "주문 당시 증권사 연결과 달라 체결 대사를 차단했습니다. 증권사 원장을 확인하세요."
                )
        data, environment = self.request(
            db,
            user,
            "kiwoom",
            "/api/dostk/acnt",
            {
                "ord_dt": local_time(order.created_at, "Asia/Seoul")[:10].replace("-", ""),
                "qry_tp": "1",
                "stk_bond_tp": "1",
                "sell_tp": "2" if value["side"] == "buy" else "1",
                "stk_cd": value["code"],
                "fr_ord_no": "",
                "dmst_stex_tp": "KRX",
            },
            "kt00007",
        )
        if environment != value["environment"] or data.get("_has_more"):
            raise HTTPException(409, "환경 변경 또는 추가 조회 필요 · 체결 확정 보류")
        rows = [
            r
            for r in data.get("acnt_ord_cntr_prps_dtl", [])
            if str(r.get("ord_no", "")).lstrip("0") == str(value["broker_order_no"]).lstrip("0")
            and str(r.get("stk_cd", "")).removeprefix("A") == value["code"]
        ]
        if len(rows) != 1:
            raise HTTPException(409, "정확한 주문 체결 기록 미확인 · 재주문하지 마세요.")
        r = rows[0]
        canceled = 0
        cancel_no = value.get("cancel_order_no")
        if cancel_no:
            confirmations = [
                c
                for c in data.get("acnt_ord_cntr_prps_dtl", [])
                if str(c.get("ord_no", "")).lstrip("0") == str(cancel_no).lstrip("0")
                and str(c.get("ori_ord", "")).lstrip("0") == str(value["broker_order_no"]).lstrip("0")
                and str(c.get("stk_cd", "")).removeprefix("A") == value["code"]
                and str(c.get("mdfy_cncl", "")).strip() == "취소확인"
            ]
            if len(confirmations) == 1:
                canceled = numeric(confirmations[0].get("cnfm_qty")) or 0
        qty, filled, remaining, price = (
            numeric(r.get(k)) for k in ("ord_qty", "cntr_qty", "ord_remnq", "cntr_uv")
        )
        side = "매수" if value["side"] == "buy" else "매도"
        if (
            side not in str(r.get("io_tp_nm", ""))
            or qty != value["quantity"]
            or filled is None
            or remaining is None
            or filled < 0
            or remaining < 0
            or canceled < 0
            or canceled > (value.get("cancel_quantity") or 0)
            or filled + remaining + canceled != qty
            or (filled and (price is None or price <= 0))
            or (r.get("mdfy_cncl", "").strip() and not canceled)
        ):
            raise HTTPException(409, "체결 수량·방향·정정취소 확인 필요 · 자동 확정 보류")
        result = {
            "filled_quantity": int(filled),
            "remaining_quantity": int(remaining),
            "fill_price": price if filled else None,
        }
        if canceled:
            result.update(canceled_quantity=int(canceled), cancel_confirmed=remaining == 0)
        return result

    def cancel(self, db, user, order):
        value = order.value
        actual = {
            "owner_email": user.email.lower(),
            "broker": value["broker"],
            "code": value["code"],
            "side": value["side"],
            "quantity": value["quantity"],
            "limit_price": value.get("limit_price"),
        }
        scope = self.config.live_order_scope
        if not self.config.live_orders_enabled or (scope and scope != actual):
            raise HTTPException(403, "승인된 주문의 잔량 취소만 가능합니다.")
        if not (0 < value["cancel_quantity"] <= value["quantity"]):
            raise HTTPException(409, "취소 가능한 영웅문 주문 잔량이 아닙니다.")
        if value["broker"] == "namuh":
            from .namuh_orders import cancel

            return cancel(self, db, user, order)
        data, environment = self.request(
            db,
            user,
            "kiwoom",
            "/api/dostk/ordr",
            {
                "dmst_stex_tp": "KRX",
                "orig_ord_no": value["broker_order_no"],
                "stk_cd": value["code"],
                "cncl_qty": str(value["cancel_quantity"]),
            },
            "kt10003",
        )
        if environment != value["environment"] or not data.get("ord_no"):
            raise HTTPException(502, "취소 응답 확인 필요 · 재전송 금지")
        return str(data["ord_no"])

    def position(self, db, user, plan, account):
        """Read broker holdings, never infer total holdings from sellable quantity."""
        self.validate_account(db, user, plan.broker, account)
        if plan.broker == "namuh":
            raw, environment = self.request(
                db,
                user,
                plan.broker,
                "/krstock/inquiry/v1/balance",
                {
                    "act_no": account,
                    "bnc_bse_cd": "1",
                    "ltg_aot_dit_cd": "1",
                    "aet_bse": "1",
                    "qut_dit_cd": "KRX",
                    "aly_qut_cd": "1",
                },
            )
            rows = raw.get("Output_1")
            partial = raw.get("_has_more", False) or raw.get("cts_flag") not in (None, "", "N", "0")
            key = "iem_cd"
        else:
            raw, environment = self.request(
                db, user, plan.broker, "/api/dostk/acnt", {"qry_tp": "1", "dmst_stex_tp": "KRX"}, "kt00018"
            )
            rows, partial, key = raw.get("acnt_evlt_remn_indv_tot"), raw.get("_has_more", False), "stk_cd"
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise HTTPException(502, "잔고 응답을 확인하지 못했습니다. 이전 수량을 유지합니다.")
        matches = [r for r in rows if str(r.get(key, "")).removeprefix("A") == plan.code]
        if partial:
            raise HTTPException(502, "잔고 연속조회 자료가 있어 보유 수량을 확정할 수 없습니다.")
        quantities = [
            holding_quantity(r) if plan.broker == "namuh" else numeric(r.get("rmnd_qty")) for r in matches
        ]
        if any(q is None or not math.isfinite(q) or q < 0 for q in quantities):
            raise HTTPException(502, "보유 수량을 확인하지 못했습니다.")
        costs = [
            (
                holding_quantity(r) if plan.broker == "namuh" else numeric(r.get("rmnd_qty")),
                numeric(r.get("phs_pr" if plan.broker == "namuh" else "pur_pric")),
            )
            for r in matches
        ]
        total = sum(quantities)
        basis = (
            sum(q * p for q, p in costs) / total
            if total and all(q is not None and p is not None and p > 0 for q, p in costs)
            else None
        )
        return {
            "quantity": total,
            "purchase_price": basis,
            "environment": environment,
            "source": "증권사 잔고",
            "quantity_basis": "broker_residual_v3" if plan.broker == "namuh" else "broker_balance_v1",
            "collected_at": raw.get("_read_at") or now().isoformat(),
        }

    @critical_reads
    def capacity(self, db, user, plan, account, side, limit_price=None):
        broker, code = plan.broker, plan.code
        self.validate_account(db, user, broker, account)
        if broker == "namuh":
            if side == "buy":
                raw, environment = self.request(
                    db,
                    user,
                    broker,
                    "/krstock/inquiry/v1/buyableQuantity",
                    {
                        "ost_dit_cd": "1",
                        "act_no": account,
                        "iem_cd": code,
                        "nmn_pr_tp_cd": "01" if limit_price is not None else "05",
                        "orr_pr": limit_price or 0,
                    },
                )
                row = raw.get("Output_0", {})
                return numeric(row.get("csh_orr_pbl_qty")), numeric(row.get("csh_orr_pbl_amt")), environment
            raw, environment = self.request(
                db,
                user,
                broker,
                "/krstock/inquiry/v1/sellableQuantity",
                {"act_no": account, "iem_cd": code, "cfd_lon_cd": "00"},
            )
            return numeric(raw.get("Output_0", {}).get("sll_pbl_qty")), None, environment
        if side == "buy":
            raw, environment = self.request(
                db,
                user,
                broker,
                "/api/dostk/acnt",
                {"stk_cd": code, "uv": str(limit_price) if limit_price is not None else ""},
                "kt00011",
            )
            cash = numeric(raw.get("ord_alowa"))
            limit = numeric(raw.get("min_ord_alow_amt"))
            if cash is None or limit is None:
                raise HTTPException(502, "미수 없는 주문가능 현금이 확인되지 않습니다.")
            return numeric(raw.get("min_ord_alowq")), min(cash, limit), environment
        raw, environment = self.request(
            db, user, broker, "/api/dostk/acnt", {"qry_tp": "1", "dmst_stex_tp": "KRX"}, "kt00018"
        )
        if raw.get("acnt_evlt_remn_indv_tot") is None:
            raise HTTPException(502, "매도가능수량 조회 실패")
        matches = [
            r for r in raw["acnt_evlt_remn_indv_tot"] if str(r.get("stk_cd", "")).removeprefix("A") == code
        ]
        return sum(numeric(r.get("trde_able_qty")) or 0 for r in matches), None, environment

    def submit(self, db, user, plan, account, side, quantity, *, limit_price=None):
        if db is not None:
            from .trade_safety import ensure_new_order_allowed

            ensure_new_order_allowed(db, user, self.config)
        scope = self.config.live_order_scope
        actual = {
            "owner_email": user.email.lower() if user else "",
            "broker": plan.broker,
            "code": plan.code,
            "side": side,
            "quantity": quantity,
            "limit_price": limit_price,
        }
        if scope and (set(scope) != set(actual) or any(actual[k] != v for k, v in scope.items())):
            raise HTTPException(403, "승인된 소액 주문 범위를 벗어났습니다.")
        if plan.broker == "namuh":
            data, _ = self.request(
                db,
                user,
                "namuh",
                "/krstock/order/v1/" + ("cashBuy" if side == "buy" else "cashSell"),
                {
                    "act_no": account,
                    "iem_cd": plan.code,
                    "orr_qty": quantity,
                    "orr_pr": limit_price if limit_price is not None else 0,
                    "nmn_pr_tp_cd": "01" if limit_price is not None else "05",
                    "orr_cnd_dit_cd": "00",
                    "ssl_nmn_pr_dit_cd": "00",
                    "rmt_mkt_cd": "KRX",
                    "sor_mkt_sli_yn": "N",
                },
            )
            order_no = data.get("Output_0", {}).get("mkt_orr_no")
        else:
            data, _ = self.request(
                db,
                user,
                "kiwoom",
                "/api/dostk/ordr",
                {
                    "dmst_stex_tp": "KRX",
                    "stk_cd": plan.code,
                    "ord_qty": str(quantity),
                    "trde_tp": "0" if limit_price is not None else "3",
                    "ord_uv": str(limit_price) if limit_price is not None else "",
                },
                "kt10000" if side == "buy" else "kt10001",
            )
            order_no = data.get("ord_no")
        if not order_no:
            raise HTTPException(502, "주문번호가 확인되지 않습니다. 재주문 전 증권사 원장을 확인하세요.")
        return str(order_no)

    def quote(self, db, user, plan):
        if plan.broker == "namuh":
            raw, environment = self.request(
                db, user, "namuh", "/krstock/quote/v1/currentPrice", {"iem_cd": plan.code, "market_cd": "KRX"}
            )
            price = numeric(raw.get("Output_0", {}).get("stck_prpr"))
        else:
            raw, environment = self.request(
                db, user, "kiwoom", "/api/dostk/stkinfo", {"stk_cd": plan.code}, "ka10001"
            )
            price = numeric(raw.get("cur_prc"))
            price = abs(price) if price is not None else None
        if price is None or price <= 0:
            raise HTTPException(502, "유효한 현재가를 확인하지 못했습니다.")
        if db is not None:
            from datetime import datetime

            stamp = (
                datetime.fromisoformat(raw["_read_at"])
                if plan.broker == "namuh" and raw.get("_read_at")
                else now()
            )
            db.add(
                BrokerSnapshot(
                    owner_id=user.id,
                    tenant_id=user.tenant_id,
                    kind="trade_quote",
                    symbol=plan.code,
                    environment=environment,
                    value={
                        "plan_id": plan.id,
                        "broker": plan.broker,
                        "price": price,
                        "collected_at": stamp.isoformat(),
                        "source": "broker_current_price",
                    },
                    created_at=stamp,
                )
            )
            db.flush()
        return price
