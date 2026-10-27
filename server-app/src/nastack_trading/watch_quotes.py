"""Page-independent NH collection; the watchlist endpoint reads only the DB."""

import logging
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .db import aware, now
from .live_worker import leader
from .market import market_router, market_session, snapshot_view
from .models import BrokerSnapshot, Instrument, User, WatchlistItem


class BatchQuotes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    codes: list[str] = Field(min_length=1, max_length=100)
    refresh: bool = False  # Compatibility only: never initiates external calls.


def owned_codes(db, user):
    return list(
        db.scalars(
            select(Instrument.code)
            .join(WatchlistItem, WatchlistItem.instrument_id == Instrument.id)
            .where(
                WatchlistItem.owner_id == user.id,
                WatchlistItem.tenant_id == user.tenant_id,
                Instrument.tenant_id == user.tenant_id,
            )
            .order_by(Instrument.code)
        )
    )


def watch_quotes_router(current_user, db_session, config, transport=None):
    router = APIRouter()

    @router.post("/api/watchlist/quotes")
    def quotes(payload: BatchQuotes, user=Depends(current_user), db=Depends(db_session)):
        codes = list(dict.fromkeys(payload.codes))
        if not set(codes).issubset(owned_codes(db, user)):
            raise HTTPException(404, "본인의 관심종목만 조회할 수 있습니다.")
        cached = {}
        for row in db.scalars(
            select(BrokerSnapshot)
            .where(
                BrokerSnapshot.owner_id == user.id,
                BrokerSnapshot.tenant_id == user.tenant_id,
                BrokerSnapshot.kind == "market",
                BrokerSnapshot.symbol.in_(codes),
            )
            .order_by(BrokerSnapshot.created_at.desc(), BrokerSnapshot.id.desc())
        ):
            cached.setdefault(row.symbol, row)
        items = {}
        for code in codes:
            row = cached.get(code)
            checked = row.value.get("watch_checked_at") if row else None
            snapshot = snapshot_view(row, config.timezone)
            if snapshot and checked:
                snapshot["collected_at"] = checked
            items[code] = {
                "snapshot": snapshot,
                "chart": row.value.get("chart", []) if row else [],
                "history_source": "나무증권 저장 일봉",
                "stale": not row
                or (
                    now() - aware(datetime.fromisoformat(checked) if checked else row.created_at)
                ).total_seconds()
                > 60,
                "error": "나무증권 시세 수집 대기" if not row else None,
            }
        return {"broker": "namuh", "is_open": market_session()["is_open"], "items": items}

    return router


class WatchQuoteWorker:
    def __init__(self, config, engine, factory, transport=None):
        self.engine, self.factory = engine, factory
        router = market_router(lambda: None, lambda: None, config, transport)
        self.refresh = next(r.endpoint for r in router.routes if r.path.endswith("/{code}/refresh"))
        self.halt = threading.Event()
        self.thread = None
        self.due = {}

    def start(self):
        self.thread = threading.Thread(target=self.run, name="watch-quotes", daemon=True)
        self.thread.start()

    def stop(self):
        self.halt.set()
        if self.thread:
            self.thread.join()

    def cycle(self):
        with self.factory() as db:
            owners = list(db.scalars(select(User.id).where(User.active.is_(True), User.role != "viewer")))
        active = set()
        for owner in owners:
            with self.factory() as db:
                user = db.get(User, owner)
                codes = owned_codes(db, user)
            for code in codes:
                key = (owner, code)
                active.add(key)
                if self.halt.is_set():
                    return
                if time.monotonic() < self.due.get(key, 0):
                    continue
                delay = 10 if market_session()["is_open"] else 300
                try:
                    with self.factory() as db:
                        user = db.get(User, owner)
                        if (
                            not user
                            or not user.active
                            or user.role == "viewer"
                            or code not in owned_codes(db, user)
                        ):
                            continue
                        opened = market_session()["is_open"]
                        self.refresh(code, auto=opened, quote_only=opened, user=user, db=db)
                        latest = db.scalar(
                            select(BrokerSnapshot)
                            .where(
                                BrokerSnapshot.owner_id == user.id,
                                BrokerSnapshot.tenant_id == user.tenant_id,
                                BrokerSnapshot.kind == "market",
                                BrokerSnapshot.symbol == code,
                            )
                            .order_by(BrokerSnapshot.created_at.desc(), BrokerSnapshot.id.desc())
                            .limit(1)
                        )
                        if latest:
                            latest.value = {**latest.value, "watch_checked_at": now().isoformat()}
                            db.commit()
                except Exception:
                    delay = 60
                    logging.getLogger(__name__).warning("Watch quote collection delayed")
                self.due[key] = time.monotonic() + delay
                if self.halt.wait(0.25):
                    return
        self.due = {key: value for key, value in self.due.items() if key in active}

    def run(self):
        while not self.halt.is_set():
            try:
                with leader(self.engine, key=2026102309) as acquired:
                    if acquired:
                        while not self.halt.is_set():
                            self.cycle()
                            if self.halt.wait(1):
                                return
            except Exception:
                logging.getLogger(__name__).warning("Watch quote collector retry pending")
            self.halt.wait(10)
