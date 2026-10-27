"""Manual domestic security registry and per-user watchlist, no price/trading data."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from .db import aware, local_time, now
from .instrument_master import download_master, save_master
from .models import Instrument, InstrumentMaster, User, WatchlistItem
from .schemas import InstrumentCreate, InstrumentUpdate, WatchlistCreate
from .security import audit


def instrument_view(item, timezone):
    return {
        "id": item.id,
        "code": item.code,
        "version": item.version,
        **item.value,
        "registration": item.value.get("registration", "manual"),
        "updated_at": local_time(item.updated_at, timezone),
    }


def instrument_changed(db, run):
    if run.kind != "research" or not run.instrument_snapshot:
        return False
    item = db.get(Instrument, run.instrument_snapshot["id"])
    return not item or item.version != run.instrument_snapshot["version"] or item.value["status"] != "active"


def instruments_router(current_user, administrator, db_session, timezone, default_attributes=None):
    router = APIRouter()

    def paging(size):
        if size not in {5, 10}:
            raise HTTPException(422, "5개 또는 10개씩 표시하세요.")

    def mutable(user):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자입니다.")

    @router.get("/api/instrument-search")
    def search_master(
        q: str = Query("", max_length=80),
        page: int = Query(1, ge=1),
        page_size: int = Query(5),
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        paging(page_size)
        master = db.get(InstrumentMaster, user.tenant_id)
        if not master:
            return {"items": [], "total": 0, "page": page, "page_size": page_size, "updated_at": None}
        term = q.strip().casefold()
        matches = [r for r in master.value["items"] if term in r["code"] or term in r["name"].casefold()]
        codes = [r["code"] for r in matches[(page - 1) * page_size : page * page_size]]
        registered = {
            r.code: r.id
            for r in db.scalars(
                select(Instrument).where(Instrument.tenant_id == user.tenant_id, Instrument.code.in_(codes))
            )
        }
        return {
            "items": [
                {**r, "registration": "nh_public_master", "registered_id": registered.get(r["code"])}
                for r in matches[(page - 1) * page_size : page * page_size]
            ],
            "total": len(matches),
            "page": page,
            "page_size": page_size,
            "source_url": master.value["source_url"],
            "snapshot": master.value["sha256"],
            "updated_at": local_time(master.updated_at, timezone),
        }

    @router.post("/api/admin/instrument-master/refresh")
    def refresh_master(user=Depends(current_user), db=Depends(db_session)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자입니다.")
        master = db.get(InstrumentMaster, user.tenant_id)
        if master and (now() - aware(master.updated_at)).total_seconds() < 60:
            raise HTTPException(429, "목록 갱신은 1분 뒤 다시 시도하세요.")
        try:
            value = download_master()
        except Exception:
            audit(db, user, "instrument.master_refresh_failed", user.tenant_id, {"source": "NH PLUG"})
            db.commit()
            raise HTTPException(
                502, "공식 종목 목록을 가져오지 못했습니다. 기존 목록은 유지됩니다."
            ) from None
        master = save_master(db, user, value)
        db.commit()
        return {"count": len(value["items"]), "updated_at": local_time(master.updated_at, timezone)}

    @router.post("/api/admin/instrument-search/{code}", status_code=201)
    def register_selected(
        code: str,
        snapshot: str = Query(..., min_length=64, max_length=64),
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자입니다.")
        master = db.get(InstrumentMaster, user.tenant_id)
        if not master or master.value["sha256"] != snapshot:
            raise HTTPException(409, "공식 목록이 변경되었습니다. 다시 검색하세요.")
        selected = next((r for r in master.value["items"] if r["code"] == code), None)
        if not selected:
            raise HTTPException(404, "공식 목록에 없는 종목입니다.")
        value = {
            **selected,
            "registration": "nh_public_master",
            "source_note": f"NH PLUG 공식 종목 목록 · {local_time(master.updated_at, timezone)} · {master.value['source_url']}",
            "source_snapshot": snapshot,
        }
        item = Instrument(
            tenant_id=user.tenant_id, code=code, value={k: v for k, v in value.items() if k != "code"}
        )
        db.add(item)
        try:
            db.flush()
            audit(db, user, "instrument.created", item.id, {"value": value, "version": 1})
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "이미 등록된 종목입니다. 기존 정보는 변경하지 않았습니다.") from None
        return instrument_view(item, timezone)

    @router.get("/api/instruments")
    def list_instruments(
        q: str = Query("", max_length=80),
        page: int = Query(1, ge=1),
        page_size: int = Query(5),
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        paging(page_size)
        query = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        scope = [Instrument.tenant_id == user.tenant_id]
        if q.strip():
            scope.append(
                Instrument.code.ilike(query, escape="\\")
                | Instrument.value["name"].as_string().ilike(query, escape="\\")
            )
        total = db.scalar(select(func.count()).select_from(Instrument).where(*scope))
        rows = db.scalars(
            select(Instrument)
            .where(*scope)
            .order_by(Instrument.code)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return {
            "items": [instrument_view(item, timezone) for item in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @router.post("/api/admin/instruments", status_code=201)
    def add_instrument(payload: InstrumentCreate, user=Depends(administrator), db=Depends(db_session)):
        item = Instrument(
            tenant_id=user.tenant_id, code=payload.code, value=payload.model_dump(exclude={"code"})
        )
        db.add(item)
        try:
            db.flush()
            audit(db, user, "instrument.created", item.id, {"value": payload.model_dump(), "version": 1})
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "이미 등록된 종목 코드입니다.") from None
        return instrument_view(item, timezone)

    @router.put("/api/admin/instruments/{instrument_id}")
    def edit_instrument(
        instrument_id: str, payload: InstrumentUpdate, user=Depends(administrator), db=Depends(db_session)
    ):
        item = db.scalar(
            select(Instrument).where(Instrument.id == instrument_id, Instrument.tenant_id == user.tenant_id)
        )
        if not item:
            raise HTTPException(404, "종목을 찾을 수 없습니다.")
        previous = dict(item.value)
        value = payload.model_dump(exclude={"version", "reason"})
        for key in ("business_description", "business_source_url"):
            if key not in payload.model_fields_set:
                value[key] = previous.get(key, "")
        changed = db.execute(
            update(Instrument)
            .where(Instrument.id == item.id, Instrument.version == payload.version)
            .values(value=value, version=payload.version + 1, updated_at=now())
        )
        if changed.rowcount != 1:
            raise HTTPException(409, "종목 정보가 변경되었습니다. 다시 불러오세요.")
        audit(
            db,
            user,
            "instrument.updated",
            item.id,
            {"previous": previous, "value": value, "version": payload.version + 1, "reason": payload.reason},
        )
        db.commit()
        db.refresh(item)
        return instrument_view(item, timezone)

    @router.get("/api/watchlist")
    def list_watchlist(
        page: int = Query(1, ge=1),
        page_size: int = Query(5),
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        paging(page_size)
        scope = [
            WatchlistItem.owner_id == user.id,
            WatchlistItem.tenant_id == user.tenant_id,
            Instrument.tenant_id == user.tenant_id,
        ]
        query = (
            select(WatchlistItem, Instrument)
            .join(Instrument, Instrument.id == WatchlistItem.instrument_id)
            .where(*scope)
        )
        total = db.scalar(
            select(func.count())
            .select_from(WatchlistItem)
            .join(Instrument, Instrument.id == WatchlistItem.instrument_id)
            .where(*scope)
        )
        rows = db.execute(query.order_by(Instrument.code).offset((page - 1) * page_size).limit(page_size))
        return {
            "items": [
                {
                    "instrument": instrument_view(item, timezone),
                    "created_at": local_time(watch.created_at, timezone),
                }
                for watch, item in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @router.post("/api/watchlist", status_code=201)
    def add_watchlist(
        payload: WatchlistCreate,
        background: BackgroundTasks,
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        mutable(user)
        db.scalar(select(User).where(User.id == user.id).with_for_update())
        item = db.scalar(
            select(Instrument).where(
                Instrument.id == payload.instrument_id, Instrument.tenant_id == user.tenant_id
            )
        )
        if not item:
            raise HTTPException(404, "종목을 찾을 수 없습니다.")
        if item.value["status"] != "active":
            raise HTTPException(409, "사용 중지된 종목입니다.")
        if db.scalar(
            select(WatchlistItem.id).where(
                WatchlistItem.owner_id == user.id, WatchlistItem.instrument_id == item.id
            )
        ):
            raise HTTPException(409, "이미 관심종목에 있습니다.")
        if (
            db.scalar(
                select(func.count()).select_from(WatchlistItem).where(WatchlistItem.owner_id == user.id)
            )
            >= 100
        ):
            raise HTTPException(409, "관심종목은 최대 100개입니다.")
        db.add(WatchlistItem(tenant_id=user.tenant_id, owner_id=user.id, instrument_id=item.id))
        audit(db, user, "watchlist.added", item.id, {"code": item.code})
        db.commit()
        if default_attributes:
            default_attributes(db, user, [{"code": item.code, **item.value}], background)
        return {"instrument": instrument_view(item, timezone)}

    @router.delete("/api/watchlist/{instrument_id}", status_code=204)
    def remove_watchlist(instrument_id: str, user=Depends(current_user), db=Depends(db_session)):
        mutable(user)
        removed = db.execute(
            delete(WatchlistItem).where(
                WatchlistItem.owner_id == user.id,
                WatchlistItem.tenant_id == user.tenant_id,
                WatchlistItem.instrument_id == instrument_id,
            )
        )
        if not removed.rowcount:
            raise HTTPException(404, "본인 관심종목에 없습니다.")
        audit(db, user, "watchlist.removed", instrument_id)
        db.commit()
        return Response(status_code=204)

    return router
