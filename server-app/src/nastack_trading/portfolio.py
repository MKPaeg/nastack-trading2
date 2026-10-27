"""Broker-separated, owner-scoped read-only portfolio snapshots."""

import re
from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy import select

from .kiwoom import KiwoomReader
from .market import numeric, snapshot_view
from .models import BrokerSnapshot, VectorResult


class ReturnRange(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self):
        if self.start > self.end or (self.end - self.start).days > 731:
            raise ValueError("시작일부터 종료일까지 최대 2년을 선택하세요.")
        return self


def portfolio_router(current_user, db_session, config, transport=None, default_attributes=None):
    router = APIRouter()
    reader = KiwoomReader(config, transport)

    def cached(db, user, kind):
        return db.scalar(
            select(BrokerSnapshot)
            .where(
                BrokerSnapshot.owner_id == user.id,
                BrokerSnapshot.tenant_id == user.tenant_id,
                BrokerSnapshot.kind == kind,
            )
            .order_by(BrokerSnapshot.created_at.desc(), BrokerSnapshot.id.desc())
            .limit(1)
        )

    def save(db, user, kind, environment, value):
        row = BrokerSnapshot(
            owner_id=user.id,
            tenant_id=user.tenant_id,
            kind=kind,
            symbol="",
            environment=environment,
            value=value,
        )
        db.add(row)
        db.commit()
        return snapshot_view(row, config.timezone)

    @router.get("/api/portfolio/kiwoom/holdings")
    def holdings(user=Depends(current_user), db=Depends(db_session)):
        return {"snapshot": snapshot_view(cached(db, user, "kiwoom_holdings"), config.timezone)}

    @router.get("/api/portfolio/predictions")
    def predictions(user=Depends(current_user), db=Depends(db_session)):
        rows = db.scalars(
            select(VectorResult)
            .where(
                VectorResult.owner_id == user.id,
                VectorResult.tenant_id == user.tenant_id,
            )
            .order_by(VectorResult.created_at.desc())
            .limit(100)
        ).all()
        items = {}
        for row in rows:
            value = row.value
            code = value.get("request", {}).get("code")
            if code and code not in items:
                items[code] = {
                    "target": value.get("today"),
                    "cutoff": value.get("data_cutoff"),
                    "daily": next(
                        (
                            c.get("forecast")
                            for group in value.get("horizon_results", [])
                            if group.get("horizon") == 1
                            for c in group.get("today_comparison", [])
                            if c.get("method") == "pearson"
                        ),
                        None,
                    ),
                    "weekly": next(
                        (
                            c
                            for group in value.get("horizon_results", [])
                            if group.get("horizon") == 5
                            for c in group.get("forecasts", [])
                            if c.get("method") == "pearson"
                        ),
                        None,
                    ),
                    "forecasts": [
                        entry.get("forecast")
                        for entry in value.get("comparison", [])
                        if entry.get("method") == "pearson"
                    ],
                }
        return {"items": items}

    @router.post("/api/portfolio/kiwoom/holdings/refresh")
    def refresh(background: BackgroundTasks, user=Depends(current_user), db=Depends(db_session)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 외부 자료를 갱신할 수 없습니다.")
        token, environment = reader.token(db, user)
        items, continuation, seen = [], None, set()
        for _ in range(20):
            raw, headers = reader.read(
                token, environment, "kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"}, continuation
            )
            rows = raw.get("acnt_evlt_remn_indv_tot")
            if not isinstance(rows, list):
                raise HTTPException(502, "영웅문 잔고 형식 오류: 기존 기록을 유지합니다.")
            for row in rows:
                code = str(row.get("stk_cd", "")).removeprefix("A")
                if not re.fullmatch(r"\d{6}", code):
                    continue
                items.append(
                    {
                        "code": code,
                        "name": row.get("stk_nm") or code,
                        "quantity": numeric(row.get("rmnd_qty")),
                        "purchase_price": numeric(row.get("pur_pric")),
                        "price": abs(numeric(row.get("cur_prc")))
                        if numeric(row.get("cur_prc")) is not None
                        else None,
                        "valuation": numeric(row.get("evlt_amt")),
                        "profit": numeric(row.get("evltv_prft")),
                        "profit_rate": numeric(row.get("prft_rt")),
                    }
                )
            if headers.get("cont-yn") != "Y":
                break
            continuation = headers.get("next-key")
            if not continuation or continuation in seen:
                raise HTTPException(502, "영웅문 연속조회 응답 오류: 기존 기록을 유지합니다.")
            seen.add(continuation)
        value = {
            "broker": "kiwoom",
            "account_last4": "연결 계좌",
            "items": items,
            "total": len(items),
            "source": "키움 REST kt00018",
            "valuation": numeric(raw.get("tot_evlt_amt")),
            "profit": numeric(raw.get("tot_evlt_pl")),
            "profit_rate": numeric(raw.get("tot_prft_rt")),
            "partial": headers.get("cont-yn") == "Y",
        }
        result = save(db, user, "kiwoom_holdings", environment, value)
        if default_attributes:
            default_attributes(db, user, items, background)
        return {"snapshot": result}

    @router.get("/api/portfolio/kiwoom/returns")
    def returns(user=Depends(current_user), db=Depends(db_session)):
        return {"snapshot": snapshot_view(cached(db, user, "kiwoom_returns"), config.timezone)}

    @router.post("/api/portfolio/kiwoom/returns/refresh")
    def refresh_returns(payload: ReturnRange, user=Depends(current_user), db=Depends(db_session)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 외부 자료를 갱신할 수 없습니다.")
        token, environment = reader.token(db, user)
        raw, headers = reader.read(
            token,
            environment,
            "kt00016",
            {"fr_dt": payload.start.strftime("%Y%m%d"), "to_dt": payload.end.strftime("%Y%m%d")},
        )
        if headers.get("cont-yn") == "Y":
            raise HTTPException(502, "기간 수익 응답이 불완전하여 저장하지 않습니다.")
        value = {
            "start": str(payload.start),
            "end": str(payload.end),
            "source": "키움 REST kt00016 · 증권사 제공 기간 수익률",
            **{
                key: numeric(raw.get(key))
                for key in (
                    "tot_amt_fr",
                    "tot_amt_to",
                    "invt_bsamt",
                    "evltv_prft",
                    "prft_rt",
                    "termin_tot_trns",
                    "termin_tot_pymn",
                )
            },
        }
        if value["prft_rt"] is None:
            raise HTTPException(502, "증권사가 기간 수익률을 제공하지 않았습니다. 0%로 대체하지 않습니다.")
        return {"snapshot": save(db, user, "kiwoom_returns", environment, value)}

    return router
