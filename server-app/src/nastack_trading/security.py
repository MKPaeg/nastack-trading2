import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import inspect, select, text, update

from .db import aware, now
from .models import AdminSettings, AuditEvent, AuthSession, SchedulePolicy, Tenant, User, UserSettings
from .schemas import Preferences, SchedulePlan


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_code(code: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode(), salt.encode(), 600_000).hex()
    return f"{salt}${digest}"


def verify_code(code: str, encoded: str) -> bool:
    salt, digest = encoded.split("$", 1)
    candidate = hashlib.pbkdf2_hmac("sha256", code.encode(), salt.encode(), 600_000).hex()
    return hmac.compare_digest(candidate, digest)


def audit(db, user: User, action: str, target: str, detail: dict | None = None):
    db.add(
        AuditEvent(
            tenant_id=user.tenant_id, actor_id=user.id, action=action, target_id=target, detail=detail or {}
        )
    )


def session_user(db, token: str | None) -> User | None:
    if not token:
        return None
    session = db.get(AuthSession, token_digest(token))
    if (
        not session
        or session.revoked_at
        or (session.expires_at is not None and aware(session.expires_at) <= now())
    ):
        return None
    user = db.get(User, session.user_id)
    if session.expires_at is None and (not user or user.role != "admin"):
        return None
    return user if user and user.active else None


def revoke_sessions(db, condition, reason):
    db.execute(
        update(AuthSession)
        .where(condition, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now(), end_reason=reason)
    )


def client_metadata(request):
    agent = "".join(char for char in request.headers.get("user-agent", "")[:500] if char.isprintable())
    if "iPhone" in agent:
        device = "아이폰"
    elif "iPad" in agent or ("Macintosh" in agent and "Mobile/" in agent):
        device = "아이패드"
    elif "Android" in agent:
        device = "Android"
    elif "Macintosh" in agent:
        device = "Mac"
    elif "Windows" in agent:
        device = "Windows PC"
    elif "Linux" in agent:
        device = "Linux"
    else:
        device = "알 수 없는 기기"
    browser = next(
        (
            label
            for marker, label in [
                ("Edg/", "Edge"),
                ("EdgiOS/", "Edge"),
                ("OPR/", "Opera"),
                ("CriOS/", "Chrome"),
                ("Chrome/", "Chrome"),
                ("FxiOS/", "Firefox"),
                ("Firefox/", "Firefox"),
                ("Safari/", "Safari"),
            ]
            if marker in agent
        ),
        "알 수 없음",
    )
    return {
        "device": device,
        "browser": browser,
        "user_agent": agent,
        "client_ip": request.client.host[:45] if request.client else "",
    }


def create_session(db, user: User, hours: int, metadata=None) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            token_hash=token_digest(token),
            user_id=user.id,
            expires_at=None if user.role == "admin" else now() + timedelta(hours=hours),
            last_seen_at=now(),
            metadata_recorded_at=now() if metadata else None,
            **(metadata or {}),
        )
    )
    return token


def bootstrap(factory, config):
    """Explicit CLI operation; never overwrite an existing administrator credential."""
    code = config.admin_access_code.get_secret_value()
    if len(code) < 16:
        raise ValueError("TRADING_ADMIN_ACCESS_CODE must have at least 16 characters")
    with factory.begin() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(73429001)"))
        tenant = db.scalar(select(Tenant).where(Tenant.name == "Personal"))
        if tenant and db.scalar(select(User).where(User.tenant_id == tenant.id, User.role == "admin")):
            return
        if tenant is None:
            tenant = Tenant(name="Personal", timezone=config.timezone)
            db.add(tenant)
            db.flush()
        user = User(
            tenant_id=tenant.id,
            email=str(config.admin_email).lower(),
            role="admin",
            session_hours=db.get(AdminSettings, 1).default_session_hours,
            code_hash=hash_code(code),
        )
        db.add(user)
        db.flush()
        db.add(UserSettings(user_id=user.id, tenant_id=tenant.id, value=Preferences().model_dump()))
        if inspect(db.bind).has_table("schedule_policies"):
            db.add(SchedulePolicy(owner_id=user.id, tenant_id=tenant.id, value=SchedulePlan().model_dump()))
        audit(db, user, "admin.bootstrapped", user.id)


def issue_admin_code(factory, config, *, reissue=False):
    """Local operator CLI only. Return a random secret once, after transaction commit."""
    email = str(config.admin_email).lower()
    code = secrets.token_urlsafe(24)
    with factory.begin() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(73429001)"))
        tenant = db.scalar(select(Tenant).where(Tenant.name == "Personal").with_for_update())
        if tenant is None:
            if reissue:
                raise ValueError("재발급할 관리자 계정이 없습니다. --reissue 없이 최초 발급하세요.")
            tenant = Tenant(name="Personal", timezone=config.timezone)
            db.add(tenant)
            db.flush()
        admins = list(db.scalars(select(User).where(User.tenant_id == tenant.id, User.role == "admin")))
        if admins:
            if not reissue:
                raise ValueError(
                    "이미 관리자가 있습니다. 기존 코드를 사용하거나 기존 관리자 이메일로 --reissue를 지정하세요."
                )
            user = next((user for user in admins if user.email == email), None)
            if user is None or not user.active:
                raise ValueError(
                    "해당 이메일의 활성 관리자만 재발급할 수 있습니다. 계정/권한은 변경하지 않습니다."
                )
            user.code_hash = hash_code(code)
            revoke_sessions(db, AuthSession.user_id == user.id, "code_reissued")
            audit(db, user, "admin.code_reissued", user.id)
        else:
            if reissue:
                raise ValueError("재발급할 관리자 계정이 없습니다. --reissue 없이 최초 발급하세요.")
            if db.scalar(select(User).where(User.tenant_id == tenant.id, User.email == email)):
                raise ValueError("등록된 일반 사용자를 이 명령으로 관리자로 승격할 수 없습니다.")
            user = User(
                tenant_id=tenant.id,
                email=email,
                role="admin",
                code_hash=hash_code(code),
                session_hours=db.get(AdminSettings, 1).default_session_hours,
            )
            db.add(user)
            db.flush()
            db.add(UserSettings(user_id=user.id, tenant_id=tenant.id, value=Preferences().model_dump()))
            if inspect(db.bind).has_table("schedule_policies"):
                db.add(
                    SchedulePolicy(owner_id=user.id, tenant_id=tenant.id, value=SchedulePlan().model_dump())
                )
            audit(db, user, "admin.code_issued", user.id)
    return code
