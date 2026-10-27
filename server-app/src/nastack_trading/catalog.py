"""Source metadata management. No network fetching or implied access license."""

import re
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .db import local_time, now
from .models import PersonalSource, SourceCatalog, Tenant, User, UserSourceList
from .schemas import CatalogSource, CatalogUpdate
from .security import audit
from .source_visibility import successful_source_ids


def source_view(source, timezone):
    return {
        "id": source.id,
        "version": source.version,
        **source.value,
        "updated_at": local_time(source.updated_at, timezone),
    }


def ensure_personal_sources(db, user):
    """Copy defaults exactly once, including an intentionally empty list."""
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    if db.get(UserSourceList, user.id):
        return
    defaults = list(db.scalars(select(SourceCatalog).where(SourceCatalog.tenant_id == user.tenant_id)))
    for source in defaults:
        db.add(
            PersonalSource(
                tenant_id=user.tenant_id,
                owner_id=user.id,
                template_id=source.id,
                domain=source.domain,
                version=source.version,
                value=dict(source.value),
                updated_at=source.updated_at,
            )
        )
    db.add(UserSourceList(owner_id=user.id))
    audit(db, user, "source.defaults_copied", user.id, {"count": len(defaults)})
    db.flush()


def catalog_changed(db, run):
    if run.kind != "research":
        return False
    selected = [
        rule["domain"] for rule in run.settings_snapshot["sources"] if rule["disposition"] != "exclude"
    ]

    def relevant(domain):
        return any(
            domain == target or domain.endswith("." + target) or target.endswith("." + domain)
            for target in selected
        )

    previous = {
        item["id"]: item["version"] for item in run.source_catalog_snapshot if relevant(item["domain"])
    }
    current = list(
        db.scalars(
            select(PersonalSource).where(
                PersonalSource.tenant_id == run.tenant_id, PersonalSource.owner_id == run.owner_id
            )
        )
    )
    versions = {item.id: item.version for item in current}
    # Before migration 0008, runs held default IDs. Preserve historical snapshots,
    # but compare them against that owner's copied version, not the shared template.
    versions.update({item.template_id: item.version for item in current if item.template_id})
    return any(versions.get(key) != version for key, version in previous.items()) or any(
        item.value["status"] == "retired" and relevant(item.domain) for item in current
    )


def catalog_router(current_user, db_session, timezone):
    router = APIRouter()

    @router.get("/api/sources")
    def list_sources(
        page: int = Query(1, ge=1),
        page_size: int = Query(5),
        successful_only: bool = False,
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        if page_size not in {5, 10}:
            raise HTTPException(422, "5개 또는 10개씩 표시하세요.")
        ensure_personal_sources(db, user)
        db.commit()
        scope = (PersonalSource.tenant_id == user.tenant_id) & (PersonalSource.owner_id == user.id)
        all_total = db.scalar(select(func.count()).select_from(PersonalSource).where(scope))
        if successful_only:
            scope = scope & PersonalSource.id.in_(successful_source_ids(db, user))
        total = db.scalar(select(func.count()).select_from(PersonalSource).where(scope))
        rows = db.scalars(
            select(PersonalSource)
            .where(scope)
            .order_by(PersonalSource.domain)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return {
            "items": [source_view(row, timezone) for row in rows],
            "total": total,
            "hidden_count": all_total - total,
            "page": page,
            "page_size": page_size,
        }

    @router.post("/api/sources", status_code=201)
    def add_source(payload: CatalogSource, user=Depends(current_user), db=Depends(db_session)):
        ensure_personal_sources(db, user)
        row = PersonalSource(
            tenant_id=user.tenant_id, owner_id=user.id, domain=payload.domain, value=payload.model_dump()
        )
        db.add(row)
        try:
            db.flush()
            audit(db, user, "source.created", row.id, {"value": row.value, "version": 1})
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "이미 등록된 도메인입니다.") from None
        return source_view(row, timezone)

    @router.put("/api/sources/{source_id}")
    def edit_source(
        source_id: str, payload: CatalogUpdate, user=Depends(current_user), db=Depends(db_session)
    ):
        row = db.scalar(
            select(PersonalSource).where(
                PersonalSource.id == source_id,
                PersonalSource.tenant_id == user.tenant_id,
                PersonalSource.owner_id == user.id,
            )
        )
        if not row:
            raise HTTPException(404, "출처를 찾을 수 없습니다.")
        previous = dict(row.value)
        value = payload.model_dump(exclude={"version", "reason"})
        try:
            changed = db.execute(
                update(PersonalSource)
                .where(PersonalSource.id == row.id, PersonalSource.version == payload.version)
                .values(domain=payload.domain, value=value, version=payload.version + 1, updated_at=now())
            )
            if changed.rowcount != 1:
                raise HTTPException(409, "출처가 변경되었습니다. 다시 불러오세요.")
            audit(
                db,
                user,
                "source.updated",
                row.id,
                {
                    "previous": previous,
                    "value": value,
                    "reason": payload.reason,
                    "version": payload.version + 1,
                },
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "이미 등록된 도메인입니다.") from None
        db.refresh(row)
        return source_view(row, timezone)

    return router


def import_note(db, admin, path):
    """Explicit local admin operation; inserts candidates, never overwrites existing entries."""
    db.scalar(select(Tenant).where(Tenant.id == admin.tenant_id).with_for_update())
    existing = set(db.scalars(select(SourceCatalog.domain).where(SourceCatalog.tenant_id == admin.tenant_id)))
    region, added = "KR", 0
    for line in path.read_text().splitlines():
        if line.startswith("## 해외"):
            region = "GLOBAL"
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 6:
            continue
        for name, url in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", cells[-1]):
            domain = urlparse(url).hostname
            if not domain or domain in existing:
                continue
            payload = CatalogSource(
                name=name,
                domain=domain,
                url=url,
                region=region,
                category=cells[0],
                method="api" if "API" in cells[4] and "없음" not in cells[4] else "web",
                access_notes=f"기준 노트: {cells[4]} · 신뢰도 {cells[3]} (미검증)",
            )
            row = SourceCatalog(tenant_id=admin.tenant_id, domain=domain, value=payload.model_dump())
            db.add(row)
            db.flush()
            audit(
                db, admin, "source.imported", row.id, {"value": row.value, "version": 1, "basis": path.name}
            )
            existing.add(domain)
            added += 1
    return added
