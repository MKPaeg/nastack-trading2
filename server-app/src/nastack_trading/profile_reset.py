"""Explicit one-shot, owner-scoped business research and dimension reset.

No recurring consent is changed. Previous profiles remain in the audit log.
Never import or call broker/order/notification execution from this workflow.
"""

from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import select, update

from .business_research import ResearchRequest, run_research
from .db import now
from .default_attributes import generate, owned_instruments
from .models import AuditEvent, StockFactorProfile, User, UserSettings
from .research import ResearchError
from .security import audit


def reset_owned_profiles(
    factory,
    config,
    owner_id,
    tenant_id,
    batch_id,
    *,
    confirmed=False,
    transport=None,
    progress=None,
    codes=None,
):
    """Caller must obtain explicit paid-call and manual-reset approval for this batch."""
    if not confirmed or not batch_id or len(batch_id) > 60:
        raise ValueError("Explicit one-shot reset approval and batch ID required")
    emit = progress or (lambda value: None)
    with factory() as db:
        user = db.get(User, owner_id)
        if not user or user.tenant_id != tenant_id or not user.active or user.role == "viewer":
            raise PermissionError("Owner is inactive, read-only, or outside tenant")
        settings = db.get(UserSettings, owner_id)
        if not settings or not settings.value.get("default_attributes_enabled", True):
            raise ValueError("Default dimension generation disabled")
        items = owned_instruments(db, user, include_trade_plans=False)
        if codes is not None:
            if set(codes) - {item["code"] for item in items}:
                raise PermissionError("Requested code is not in owner's watchlist or holdings")
            items = [item for item in items if item["code"] in codes]
        baseline_settings_version = settings.version
        audits = db.scalars(
            select(AuditEvent).where(
                AuditEvent.actor_id == owner_id,
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.action == "vector.reset_attempted",
            )
        ).all()
        attempted = {row.target_id for row in audits if row.detail.get("batch_id") == batch_id}
    results = []
    for item in items:
        code = item["code"]
        if code in attempted:
            result = {"code": code, "status": "already_attempted"}
            results.append(result)
            emit(result)
            continue
        with factory() as db:
            db.execute(
                update(User)
                .where(User.id == owner_id, User.tenant_id == tenant_id)
                .values(active=User.active)
            )
            prior_attempts = db.scalars(
                select(AuditEvent).where(
                    AuditEvent.actor_id == owner_id,
                    AuditEvent.tenant_id == tenant_id,
                    AuditEvent.action == "vector.reset_attempted",
                    AuditEvent.target_id == code,
                )
            ).all()
            if any(event.detail.get("batch_id") == batch_id for event in prior_attempts):
                result = {"code": code, "status": "already_attempted"}
                results.append(result)
                emit(result)
                continue
            user = db.get(User, owner_id)
            settings = db.get(UserSettings, owner_id)
            current = (
                next(
                    (i for i in owned_instruments(db, user, include_trade_plans=False) if i["code"] == code),
                    None,
                )
                if user and user.tenant_id == tenant_id
                else None
            )
            if (
                not user
                or not user.active
                or user.role == "viewer"
                or not settings
                or settings.version != baseline_settings_version
                or not current
                or current["name"] != item["name"]
            ):
                results.append({"code": code, "status": "stopped", "error": "OWNER_OR_POLICY_CHANGED"})
                break
            # Durable before external calls. Re-running the same batch never repeats unknown paid work.
            audit(db, user, "vector.reset_attempted", code, {"batch_id": batch_id})
            db.commit()
            emit({"code": code, "name": item["name"], "status": "researching"})
            try:
                researched = run_research(
                    ResearchRequest(
                        code=code,
                        request_key=f"reset-{batch_id}-{code}",
                        confirmed=True,
                        cloud_confirmed=True,
                    ),
                    user,
                    db,
                    config,
                    transport,
                    per_instrument_cooldown=True,
                )
            except (HTTPException, ResearchError) as exc:
                result = {
                    "code": code,
                    "status": "stopped",
                    "error": exc.code
                    if isinstance(exc, ResearchError)
                    else f"REQUEST_REJECTED_{exc.status_code}",
                }
                results.append(result)
                emit(result)
                break
            if researched["status"] != "completed":
                result = {"code": code, "status": "business_failed", "error": researched.get("error")}
                results.append(result)
                emit(result)
                if researched.get("error") == "BUSINESS_PROVIDER_FAILED" or str(
                    researched.get("error", "")
                ).startswith("PROVIDER_"):
                    break
                continue
            db.expire_all()
            user = db.get(User, owner_id)
            settings = db.get(UserSettings, owner_id)
            if not user.active or user.role == "viewer" or settings.version != baseline_settings_version:
                results.append({"code": code, "status": "stopped", "error": "OWNER_OR_POLICY_CHANGED"})
                break
            profile = db.get(StockFactorProfile, (owner_id, code), with_for_update=True)
            if profile and profile.tenant_id != tenant_id:
                raise PermissionError("Profile tenant mismatch")
            before = deepcopy(profile.value) if profile else None
            audit(db, user, "vector.reset_backup", code, {"batch_id": batch_id, "before": before})
            if not profile:
                profile = StockFactorProfile(owner_id=owner_id, tenant_id=tenant_id, code=code)
                db.add(profile)
            profile.value = {
                "origin": "llm_default",
                "status": "pending",
                "instrument": item,
                "reset_batch": batch_id,
            }
            profile.updated_at = now()
            db.commit()
        generate(factory, config, owner_id, [code], transport, cloud_confirmed=True)
        with factory() as db:
            profile = db.get(StockFactorProfile, (owner_id, code))
            result = {
                "code": code,
                "status": profile.value.get("status"),
                "dimensions": profile.value.get("selected_dimensions", []),
                "error": profile.value.get("error"),
            }
        results.append(result)
        emit(result)
    return {"batch_id": batch_id, "total": len(items), "results": results}
