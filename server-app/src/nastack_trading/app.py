import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .brokers import brokers_router
from .business_research import research_router
from .catalog import catalog_router, ensure_personal_sources, source_view
from .collection_pipeline import collection_router
from .config import ROOT, Config
from .data_collection import DataCollector, data_collection_router
from .db import aware, connect, local_time, now
from .default_attributes import defaults_router
from .default_attributes import schedule as schedule_defaults
from .dimensions import dimensions_router
from .email_lists import email_lists_router
from .instruments import instrument_view, instruments_router
from .jobs import ACTIVE, log, run_view
from .kiwoom import kiwoom_router
from .live_worker import LiveWorker
from .llm_tasks import prompt_snapshot, task_policy, tasks_router
from .llm_usage_history import llm_usage_router
from .mail import send_login_code
from .market import market_router
from .models import (
    AdminSettings,
    AnalysisRun,
    AuditEvent,
    AuthSession,
    Instrument,
    LoginChallenge,
    LoginThrottle,
    PersonalSource,
    RememberedLogin,
    SchedulePolicy,
    SystemLog,
    Tenant,
    User,
    UserSettings,
)
from .news import news_router
from .opendart import opendart_router
from .paper import paper_router
from .personal_connections import connections_router
from .portfolio import portfolio_router
from .realized_sales import realized_router
from .recommendations import recommendations_router
from .reports import reports_router
from .schemas import (
    AdminPolicyUpdate,
    CodeRequest,
    Login,
    Preferences,
    RunCreate,
    SchedulePlan,
    SchedulePlanUpdate,
    SettingsUpdate,
    UserCreate,
    UserUpdate,
)
from .security import (
    audit,
    client_metadata,
    create_session,
    hash_code,
    revoke_sessions,
    session_user,
    token_digest,
    verify_code,
)
from .statistics import statistics_router
from .telegram_settings import telegram_router
from .trade_audit import clean as clean_trading_audit
from .trade_gateway import TradeGateway
from .trading import trading_router
from .user_brokers import screen_state_router, user_brokers_router
from .vectors import vectors_router
from .watch_quotes import WatchQuoteWorker, watch_quotes_router

COOKIE = "trading_session"
REMEMBER_COOKIE = "trading_remembered_login"
CHALLENGE_COOKIE = "trading_login_challenge"


def user_view(user):
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "active": user.active,
        "session_hours": user.session_hours,
        "session_unlimited": user.role == "admin",
    }


def create_app(
    config: Config | None = None,
    mail_sender=send_login_code,
    broker_transport=None,
    dimension_transport=None,
    kiwoom_transport=None,
    news_transport=None,
):
    config = config or Config()
    engine, factory = connect(config.database_url)
    # Constant work for unknown users, preventing email lookup by password timing.
    dummy_hash = hash_code(secrets.token_urlsafe(24))
    trade_gateway = TradeGateway(config, broker_transport, kiwoom_transport)
    live_worker = LiveWorker(config, engine, factory, trade_gateway)
    watch_worker = WatchQuoteWorker(config, engine, factory, broker_transport)
    data_worker = DataCollector(config, engine, factory, dimension_transport, broker_transport,
                                kiwoom_transport, news_transport)

    @asynccontextmanager
    async def lifespan(app):
        try:
            live_worker.start()
            data_worker.start()
            if broker_transport is None:
                watch_worker.start()
            yield
        finally:
            await asyncio.to_thread(data_worker.stop)
            await asyncio.to_thread(live_worker.stop)
            await asyncio.to_thread(watch_worker.stop)
            engine.dispose()

    app = FastAPI(
        title="NaStack Trading",
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.factory = factory
    app.state.live_worker = live_worker
    app.state.data_worker = data_worker

    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path.endswith("/keys") or request.url.path.startswith("/api/user/connections/"):
            return JSONResponse(
                status_code=422,
                content={"detail": "키 입력 형식·길이·버전을 확인하세요. 비밀값은 반환하지 않습니다."},
            )
        return await request_validation_exception_handler(request, error)

    @app.middleware("http")
    async def secure_requests(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            from .server import private_same_origin

            origin = request.headers.get("origin")
            lan_allowed = config.development_lan_same_origin and private_same_origin(origin, request.url)
            if origin not in config.allowed_origins and not lan_allowed:
                return Response("허용되지 않은 요청 출처", status_code=403)
        import json

        from .llm_usage import usage_scope, usage_snapshot
        from .llm_usage_history import save_usage

        category = "collection" if request.url.path.startswith(("/api/news/collect", "/api/business-research")) else "evaluation"
        with usage_scope(category) as ledger:
            response = await call_next(request)
            if request.method == "POST":
                snapshot = await asyncio.to_thread(usage_snapshot, ledger) if ledger else usage_snapshot(ledger)
                response.headers["X-LLM-Usage"] = json.dumps(snapshot, separators=(",", ":"))
            if ledger:
                owner_id = getattr(request.state, "llm_owner_id", None)
                tenant_id = getattr(request.state, "llm_tenant_id", None)
                if not owner_id:
                    with factory() as db:
                        current = session_user(db, request.cookies.get(COOKIE))
                        if current:
                            owner_id, tenant_id = current.id, current.tenant_id
                if owner_id and tenant_id:
                    try:
                        await asyncio.to_thread(save_usage, factory, owner_id, tenant_id, ledger)
                    except Exception:
                        logging.getLogger(__name__).warning("LLM usage history could not be saved")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    def db_session():
        with factory() as db:
            yield db

    def current_user(request: Request):
        # Close the authentication DB session before opening a long-lived SSE stream.
        with factory() as db:
            user = session_user(db, request.cookies.get(COOKIE))
            if user:
                session = db.get(AuthSession, token_digest(request.cookies[COOKIE]))
                metadata_changed = not session.metadata_recorded_at and bool(
                    request.headers.get("user-agent")
                )
                if metadata_changed:
                    for name, value in client_metadata(request).items():
                        setattr(session, name, value)
                    session.metadata_recorded_at = now()
                if not session.last_seen_at or aware(session.last_seen_at) < now() - timedelta(minutes=1):
                    session.last_seen_at = now()
                    db.commit()
                elif metadata_changed:
                    db.commit()
        if not user:
            raise HTTPException(401, "로그인이 필요합니다.")
        request.state.llm_owner_id = user.id
        request.state.llm_tenant_id = user.tenant_id
        return user

    def administrator(user=Depends(current_user)):
        if user.role != "admin":
            raise HTTPException(403, "관리자 권한이 필요합니다.")
        return user

    def scoped_run(db, user, run_id):
        run = db.scalar(
            select(AnalysisRun).where(
                AnalysisRun.id == run_id,
                AnalysisRun.owner_id == user.id,
                AnalysisRun.tenant_id == user.tenant_id,
            )
        )
        if not run:
            raise HTTPException(404, "작업을 찾을 수 없습니다.")
        return run

    app.include_router(catalog_router(current_user, db_session, config.timezone))

    def default_attributes(db, user, instruments, background):
        schedule_defaults(db, user, instruments, background, factory, config, news_transport)

    app.include_router(
        instruments_router(current_user, administrator, db_session, config.timezone, default_attributes)
    )
    app.include_router(telegram_router(current_user, db_session, config))
    app.include_router(
        paper_router(current_user, db_session, TradeGateway(config, broker_transport, kiwoom_transport))
    )
    app.include_router(email_lists_router(current_user, db_session, config))

    def broker_user(user=Depends(current_user)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 연결 설정을 변경할 수 없습니다.")
        return user

    app.include_router(user_brokers_router(current_user, db_session, config))
    app.include_router(screen_state_router(current_user, db_session))
    app.include_router(connections_router(current_user, db_session, config, dimension_transport))
    app.include_router(opendart_router(current_user, db_session, config, dimension_transport))
    app.include_router(brokers_router(broker_user, db_session, config, broker_transport))
    app.include_router(market_router(current_user, db_session, config, broker_transport, default_attributes))
    app.include_router(kiwoom_router(broker_user, db_session, config, kiwoom_transport))
    app.include_router(recommendations_router(current_user, db_session, config, kiwoom_transport,
                                              factory=factory, news_transport=news_transport))
    app.include_router(watch_quotes_router(current_user, db_session, config))
    app.include_router(realized_router(current_user, db_session))
    app.include_router(reports_router(current_user, db_session, config))
    app.include_router(tasks_router(current_user, db_session, config))
    app.include_router(research_router(current_user, db_session, config, news_transport))
    app.include_router(
        portfolio_router(current_user, db_session, config, kiwoom_transport, default_attributes)
    )
    app.include_router(trading_router(current_user, db_session, config, trade_gateway, live_worker))
    app.include_router(
        dimensions_router(
            current_user, db_session, config, dimension_transport, broker_transport, kiwoom_transport
        )
    )
    app.include_router(vectors_router(current_user, db_session, config, kiwoom_transport, broker_transport))
    app.include_router(
        defaults_router(current_user, broker_user, db_session, factory, config, news_transport)
    )
    app.include_router(news_router(current_user, db_session, config, news_transport))
    app.include_router(collection_router(current_user, db_session, factory, config, news_transport))
    app.include_router(statistics_router(current_user, db_session, config, news_transport))
    app.include_router(llm_usage_router(current_user, db_session))
    app.include_router(data_collection_router(current_user, db_session))

    @app.get("/api/health")
    def health(db=Depends(db_session)):
        try:
            db.scalar(select(func.count()).select_from(Tenant))
        except Exception:
            raise HTTPException(503, "DB 연결 또는 migration을 확인하세요.") from None
        return {
            "status": "ok",
            "mode": "paper",
            "analysis": "research" if config.research_enabled else "pipeline_demo",
            "timezone": config.timezone,
        }

    @app.get("/api/integrations")
    def integrations(user=Depends(current_user), db=Depends(db_session)):
        from .personal_connections import external_config, records

        scoped = external_config(db, user, config)
        # Configuration presence only: no network probe and no secrets exposed.
        return {
            "research_enabled": config.research_enabled,
            "vllm_configured": bool(config.vllm_base_url),
            "tavily_configured": bool(config.tavily_api_key.get_secret_value()),
            "openai_configured": bool(scoped.openai_api_key.get_secret_value()),
            "openai_source": "personal" if "openai" in records(db, user) else "server_shared",
            "smtp_configured": bool(scoped.smtp_host and scoped.smtp_from_email),
            "smtp_source": "personal" if "smtp" in records(db, user) else "not_configured",
            "note": "서버 설정 여부입니다. 실제 연결 성공은 분석 작업으로 확인하세요.",
        }

    @app.get("/api/auth/options")
    def auth_options():
        with factory() as db:
            policy = db.get(AdminSettings, 1)
            return {
                "default_session_hours": policy.default_session_hours,
                "max_session_hours": policy.max_session_hours,
            }

    @app.get("/api/auth/remembered-email")
    def remembered_email(request: Request, db=Depends(db_session)):
        token = request.cookies.get(REMEMBER_COOKIE)
        record = db.get(RememberedLogin, token_digest(token)) if token else None
        owner = db.get(User, record.user_id) if record and aware(record.expires_at) > now() else None
        return {"email": owner.email if owner and owner.active else ""}

    @app.delete("/api/auth/remembered-email", status_code=204)
    def forget_email(request: Request, response: Response, db=Depends(db_session)):
        token = request.cookies.get(REMEMBER_COOKIE)
        record = db.get(RememberedLogin, token_digest(token)) if token else None
        if record:
            audit(db, db.get(User, record.user_id), "auth.email_forgotten", record.user_id)
            db.delete(record)
            db.commit()
        response.delete_cookie(
            REMEMBER_COOKIE, path="/", secure=config.cookie_secure, httponly=True, samesite="strict"
        )

    @app.get("/api/admin/settings")
    def admin_settings(user=Depends(administrator), db=Depends(db_session)):
        policy = db.get(AdminSettings, 1)
        return {
            "version": policy.version,
            "default_session_hours": policy.default_session_hours,
            "max_session_hours": policy.max_session_hours,
            "code_ttl_minutes": policy.code_ttl_minutes,
            "code_max_attempts": policy.code_max_attempts,
        }

    @app.put("/api/admin/settings")
    def update_admin_settings(
        payload: AdminPolicyUpdate, user=Depends(administrator), db=Depends(db_session)
    ):
        db.scalar(select(Tenant).where(Tenant.id == user.tenant_id).with_for_update())
        policy = db.scalar(select(AdminSettings).where(AdminSettings.id == 1).with_for_update())
        if policy.version != payload.version:
            raise HTTPException(409, "관리자 설정이 변경되었습니다. 다시 불러오세요.")
        if db.scalar(select(User.id).where(User.session_hours > payload.max_session_hours).limit(1)):
            raise HTTPException(409, "먼저 사용자별 세션 시간을 새 최대 시간 이하로 조정하세요.")
        previous = {
            "default_session_hours": policy.default_session_hours,
            "max_session_hours": policy.max_session_hours,
            "code_ttl_minutes": policy.code_ttl_minutes,
            "code_max_attempts": policy.code_max_attempts,
        }
        policy.default_session_hours, policy.max_session_hours = (
            payload.default_session_hours,
            payload.max_session_hours,
        )
        policy.version += 1
        policy.code_ttl_minutes, policy.code_max_attempts = (
            payload.code_ttl_minutes,
            payload.code_max_attempts,
        )
        audit(
            db,
            user,
            "admin.settings_updated",
            "session_policy",
            {"previous": previous, "value": payload.model_dump()},
        )
        db.commit()
        return {
            "version": policy.version,
            "default_session_hours": policy.default_session_hours,
            "max_session_hours": policy.max_session_hours,
            "code_ttl_minutes": policy.code_ttl_minutes,
            "code_max_attempts": policy.code_max_attempts,
        }

    @app.post("/api/auth/login")
    def login(payload: Login, request: Request, response: Response, db=Depends(db_session)):
        if not config.legacy_access_code_login_enabled:
            raise HTTPException(410, "이메일 인증 코드를 요청한 후 확인하세요.")
        tenant = db.scalar(select(Tenant).where(Tenant.name == "Personal").with_for_update())
        if not tenant:
            raise HTTPException(503, "초기 관리자 설정이 필요합니다.")
        # Shared DB throttle survives API restarts and multiple API processes.
        address = request.client.host if request.client else "unknown"
        bucket_id = token_digest(tenant.id + address)
        bucket = db.get(LoginThrottle, bucket_id)
        if not bucket:
            bucket = LoginThrottle(bucket=bucket_id, attempts=0, window_at=now())
            db.add(bucket)
        if aware(bucket.window_at) < now() - timedelta(minutes=5):
            bucket.window_at, bucket.attempts = now(), 0
        if bucket.attempts >= 10:
            raise HTTPException(429, "잠시 후 다시 로그인하세요.")
        bucket.attempts += 1
        user = db.scalar(
            select(User).where(User.tenant_id == tenant.id, User.email == str(payload.email).lower())
        )
        valid = verify_code(payload.access_code, user.code_hash if user else dummy_hash)
        if not valid or not user or not user.active:
            if user:
                audit(db, user, "auth.failed", user.id)
            db.commit()
            raise HTTPException(401, "이메일 또는 접근 코드를 확인하세요.")
        return finish_login(user, payload, request, response, db)

    def finish_login(user, payload, request, response, db):
        hours = user.session_hours
        old = request.cookies.get(COOKIE)
        if old:
            revoke_sessions(db, AuthSession.token_hash == token_digest(old), "replaced")
        token = create_session(db, user, hours, client_metadata(request))
        expires_at = db.get(AuthSession, token_digest(token)).expires_at
        old_remember = request.cookies.get(REMEMBER_COOKIE)
        if old_remember:
            db.execute(
                delete(RememberedLogin).where(RememberedLogin.token_hash == token_digest(old_remember))
            )
        if payload.remember_email:
            remember_token = secrets.token_urlsafe(32)
            db.add(
                RememberedLogin(
                    token_hash=token_digest(remember_token),
                    user_id=user.id,
                    expires_at=now() + timedelta(days=365),
                )
            )
            audit(db, user, "auth.email_remembered", user.id)
        audit(db, user, "auth.login", user.id)
        db.commit()
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            secure=config.cookie_secure,
            samesite="strict",
            max_age=365 * 86400 if user.role == "admin" else hours * 3600,
            path="/",
        )
        if payload.remember_email:
            response.set_cookie(
                REMEMBER_COOKIE,
                remember_token,
                max_age=365 * 86400,
                path="/",
                secure=config.cookie_secure,
                httponly=True,
                samesite="strict",
            )
        else:
            response.delete_cookie(
                REMEMBER_COOKIE, path="/", secure=config.cookie_secure, httponly=True, samesite="strict"
            )
        return {**user_view(user), "session_expires_at": local_time(expires_at, config.timezone)}

    @app.post("/api/auth/request-code", status_code=202)
    def request_code(payload: CodeRequest, request: Request, response: Response, db=Depends(db_session)):
        if not config.smtp_host or not config.smtp_from_email:
            raise HTTPException(503, "메일 서버 설정이 필요합니다. 관리자에게 문의하세요.")
        tenant = db.scalar(select(Tenant).where(Tenant.name == "Personal").with_for_update())
        if not tenant:
            raise HTTPException(503, "서버 초기 관리자 등록이 필요합니다.")
        # Both address and recipient limits persist across API processes/restarts.
        address = request.client.host if request.client else "unknown"
        buckets = [("mail-ip:" + address, 10, 300), ("mail-email:" + str(payload.email).lower(), 1, 60)]
        for key, limit, seconds in buckets:
            bucket_id = token_digest(tenant.id + key)
            bucket = db.get(LoginThrottle, bucket_id)
            if not bucket:
                bucket = LoginThrottle(bucket=bucket_id, attempts=0, window_at=now())
                db.add(bucket)
            if aware(bucket.window_at) < now() - timedelta(seconds=seconds):
                bucket.attempts, bucket.window_at = 0, now()
            if bucket.attempts >= limit:
                raise HTTPException(429, "코드 재요청은 잠시 후 가능합니다.")
            bucket.attempts += 1
        user = db.scalar(
            select(User).where(
                User.tenant_id == tenant.id, User.email == str(payload.email).lower(), User.active.is_(True)
            )
        )
        token = secrets.token_urlsafe(32)
        policy = db.get(AdminSettings, 1)
        ttl = policy.code_ttl_minutes
        old = request.cookies.get(CHALLENGE_COOKIE)
        if old:
            db.execute(delete(LoginChallenge).where(LoginChallenge.token_hash == token_digest(old)))
        code = f"{secrets.randbelow(1_000_000):06d}"
        if user:
            db.add(
                LoginChallenge(
                    token_hash=token_digest(token),
                    user_id=user.id,
                    code_hash=hash_code(code),
                    expires_at=now() + timedelta(minutes=ttl),
                )
            )
            audit(db, user, "auth.code_requested", user.id)
        db.commit()  # Do not hold DB locks during SMTP.
        if user:
            try:
                mail_sender(config, user.email, code, ttl)
            except Exception:
                challenge = db.get(LoginChallenge, token_digest(token))
                if challenge:
                    db.delete(challenge)
                audit(db, user, "auth.code_delivery_failed", user.id)
                db.commit()
                raise HTTPException(503, "인증 이메일을 보내지 못했습니다. 관리자에게 문의하세요.") from None
            challenge = db.get(LoginChallenge, token_digest(token))
            if challenge:
                challenge.delivered = True
                audit(db, user, "auth.code_sent", user.id)
                db.commit()
        response.set_cookie(
            CHALLENGE_COOKIE,
            token,
            max_age=ttl * 60,
            path="/",
            httponly=True,
            secure=config.cookie_secure,
            samesite="strict",
        )
        return {
            "message": "등록된 이메일이면 인증 코드를 발송했습니다. 받은 메일을 확인하세요.",
            "code_ttl_minutes": ttl,
            "retry_after_seconds": 60,
        }

    @app.post("/api/auth/verify")
    def verify_email_code(payload: Login, request: Request, response: Response, db=Depends(db_session)):
        tenant = db.scalar(select(Tenant).where(Tenant.name == "Personal").with_for_update())
        token = request.cookies.get(CHALLENGE_COOKIE)
        challenge = db.get(LoginChallenge, token_digest(token)) if token else None
        user = db.get(User, challenge.user_id) if challenge else None
        if (
            not tenant
            or not challenge
            or not challenge.delivered
            or not user
            or not user.active
            or user.tenant_id != tenant.id
            or user.email != str(payload.email).lower()
        ):
            raise HTTPException(401, "이메일 코드를 먼저 요청하세요.")
        policy = db.get(AdminSettings, 1)
        if aware(challenge.expires_at) <= now() or challenge.attempts >= policy.code_max_attempts:
            db.delete(challenge)
            audit(db, user, "auth.code_expired_or_locked", user.id)
            db.commit()
            raise HTTPException(401, "코드가 만료되었거나 시도 횟수를 초과했습니다. 다시 요청하세요.")
        challenge.attempts += 1
        if not verify_code(payload.access_code, challenge.code_hash):
            audit(db, user, "auth.code_failed", user.id)
            db.commit()
            raise HTTPException(401, "인증 코드를 확인하세요.")
        db.delete(challenge)
        audit(db, user, "auth.code_verified", user.id)
        result = finish_login(user, payload, request, response, db)
        response.delete_cookie(
            CHALLENGE_COOKIE, path="/", secure=config.cookie_secure, httponly=True, samesite="strict"
        )
        return result

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request, response: Response, user=Depends(current_user), db=Depends(db_session)):
        revoke_sessions(db, AuthSession.token_hash == token_digest(request.cookies[COOKIE]), "logout")
        audit(db, user, "auth.logout", user.id)
        db.commit()
        response.delete_cookie(
            COOKIE, path="/", secure=config.cookie_secure, httponly=True, samesite="strict"
        )

    @app.get("/api/me")
    def me(request: Request, response: Response, user=Depends(current_user), db=Depends(db_session)):
        session = db.get(AuthSession, token_digest(request.cookies[COOKIE]))
        if session:
            response.set_cookie(
                COOKIE,
                request.cookies[COOKIE],
                httponly=True,
                secure=config.cookie_secure,
                samesite="strict",
                path="/",
                max_age=365 * 86400
                if session.expires_at is None
                else max(1, int((aware(session.expires_at) - now()).total_seconds())),
            )
        return {
            **user_view(user),
            "timezone": config.timezone,
            "mode": "paper",
            "session_expires_at": local_time(session.expires_at, config.timezone) if session else None,
        }

    @app.get("/api/settings")
    def read_settings(user=Depends(current_user), db=Depends(db_session)):
        settings = db.get(UserSettings, user.id)
        return {"version": settings.version, "value": settings.value}

    @app.put("/api/settings")
    def save_settings(payload: SettingsUpdate, user=Depends(current_user), db=Depends(db_session)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자입니다.")
        current = db.scalar(
            select(UserSettings)
            .where(UserSettings.user_id == user.id, UserSettings.tenant_id == user.tenant_id)
            .with_for_update()
        )
        merged = {**(current.value if current else {}), **payload.value.model_dump()}
        result = db.execute(
            update(UserSettings)
            .where(
                UserSettings.user_id == user.id,
                UserSettings.tenant_id == user.tenant_id,
                UserSettings.version == payload.version,
            )
            .values(value=merged, version=payload.version + 1, updated_at=now())
        )
        if result.rowcount != 1:
            raise HTTPException(409, "설정이 변경되었습니다. 다시 불러오세요.")
        audit(db, user, "settings.updated", user.id, {"version": payload.version + 1})
        log(db, user, None, "사용자 설정 저장")
        db.commit()
        return {"version": payload.version + 1, "value": payload.value.model_dump()}

    @app.get("/api/schedule-plan")
    def read_schedule_plan(user=Depends(current_user), db=Depends(db_session)):
        policy = db.get(SchedulePolicy, user.id)
        if not policy or policy.tenant_id != user.tenant_id:
            raise HTTPException(404, "정기 작업 계획을 찾지 못했습니다. DB migration을 확인하세요.")
        return {"version": policy.version, "value": policy.value, "execution_state": "not_connected"}

    @app.put("/api/schedule-plan")
    def save_schedule_plan(payload: SchedulePlanUpdate, user=Depends(current_user), db=Depends(db_session)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자는 정기 작업 계획을 변경할 수 없습니다.")
        result = db.execute(
            update(SchedulePolicy)
            .where(
                SchedulePolicy.owner_id == user.id,
                SchedulePolicy.tenant_id == user.tenant_id,
                SchedulePolicy.version == payload.version,
            )
            .values(value=payload.value.model_dump(), version=payload.version + 1, updated_at=now())
        )
        if result.rowcount != 1:
            raise HTTPException(409, "정기 작업 계획이 변경되었습니다. 다시 불러오세요.")
        audit(db, user, "schedule.plan_updated", user.id, {"version": payload.version + 1})
        db.commit()
        return {
            "version": payload.version + 1,
            "value": payload.value.model_dump(),
            "execution_state": "not_connected",
        }

    @app.post("/api/runs", status_code=202)
    def create_run(payload: RunCreate, user=Depends(current_user), db=Depends(db_session)):
        if user.role == "viewer":
            raise HTTPException(403, "조회 전용 사용자입니다.")
        # Serialize enqueue limits per owner; idempotency is enforced by the DB as well.
        db.scalar(select(User).where(User.id == user.id).with_for_update())
        existing = db.scalar(
            select(AnalysisRun).where(
                AnalysisRun.owner_id == user.id, AnalysisRun.request_key == payload.request_key
            )
        )
        if existing:
            if existing.symbol != payload.symbol:
                raise HTTPException(409, "같은 요청 키에 다른 종목을 사용할 수 없습니다.")
            return run_view(existing, config.timezone)
        pending = db.scalar(
            select(func.count())
            .select_from(AnalysisRun)
            .where(AnalysisRun.owner_id == user.id, AnalysisRun.status.in_(ACTIVE))
        )
        if pending >= 20:
            raise HTTPException(429, "대기 작업이 많습니다. 완료 후 다시 요청하세요.")
        settings = db.get(UserSettings, user.id)
        ensure_personal_sources(db, user)
        catalog = list(
            db.scalars(
                select(PersonalSource)
                .where(PersonalSource.tenant_id == user.tenant_id, PersonalSource.owner_id == user.id)
                .order_by(PersonalSource.domain)
            )
        )
        snapshot = dict(settings.value)
        rules = {rule["domain"]: dict(rule) for rule in snapshot["sources"]}
        for source in catalog:
            if source.value["status"] == "retired":
                rules[source.domain] = {"domain": source.domain, "disposition": "exclude"}
        if len(rules) > 100:
            raise HTTPException(409, "출처 정책이 100개를 초과했습니다. 관리자와 목록을 정리하세요.")
        snapshot["sources"] = list(rules.values())
        snapshot["_research_prompt"] = prompt_snapshot(task_policy(db, user, "research"))
        instrument = db.scalar(
            select(Instrument).where(
                Instrument.tenant_id == user.tenant_id, Instrument.code == payload.symbol
            )
        )
        if instrument and instrument.value["status"] != "active":
            raise HTTPException(409, "사용 중지된 종목입니다. 관리자에게 확인하세요.")
        if config.research_enabled and not instrument:
            raise HTTPException(422, "실제 분석은 관리자에 등록된 종목만 요청할 수 있습니다.")
        run = AnalysisRun(
            kind="research" if config.research_enabled else "pipeline_demo",
            tenant_id=user.tenant_id,
            owner_id=user.id,
            request_key=payload.request_key,
            symbol=payload.symbol,
            settings_version=settings.version,
            settings_snapshot=snapshot,
            source_catalog_snapshot=[source_view(source, config.timezone) for source in catalog],
            instrument_snapshot=instrument_view(instrument, config.timezone) if instrument else {},
        )
        db.add(run)
        db.flush()
        audit(db, user, "analysis.queued", run.id)
        log(db, user, run.id, "분석 작업 요청" if config.research_enabled else "검증용 분석 작업 요청")
        db.commit()
        return run_view(run, config.timezone)

    @app.get("/api/runs")
    def list_runs(
        page: int = Query(1, ge=1),
        page_size: int = Query(5, ge=5, le=10),
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        if page_size not in (5, 10):
            raise HTTPException(422, "5개 또는 10개씩 표시하세요.")
        scope = (AnalysisRun.owner_id == user.id, AnalysisRun.tenant_id == user.tenant_id)
        total = db.scalar(select(func.count()).select_from(AnalysisRun).where(*scope))
        rows = db.scalars(
            select(AnalysisRun)
            .where(*scope)
            .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return {
            "items": [run_view(row, config.timezone) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @app.get("/api/runs/{run_id}")
    def read_run(run_id: str, user=Depends(current_user), db=Depends(db_session)):
        return run_view(scoped_run(db, user, run_id), config.timezone)

    @app.get("/api/logs")
    def read_logs(
        page: int = Query(1, ge=1),
        page_size: int = Query(5, ge=5, le=10),
        user=Depends(current_user),
        db=Depends(db_session),
    ):
        if page_size not in (5, 10):
            raise HTTPException(422, "5개 또는 10개씩 표시하세요.")
        scope = (SystemLog.owner_id == user.id, SystemLog.tenant_id == user.tenant_id)
        total = db.scalar(select(func.count()).select_from(SystemLog).where(*scope))
        rows = db.scalars(
            select(SystemLog)
            .where(*scope)
            .order_by(SystemLog.created_at.desc(), SystemLog.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return {
            "items": [
                {
                    "id": row.id,
                    "level": row.level,
                    "message": row.message,
                    "run_id": row.run_id,
                    "created_at": local_time(row.created_at, config.timezone),
                }
                for row in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @app.delete("/api/logs")
    def prune_logs(
        older_than_days: int = Query(30, ge=1, le=3650), user=Depends(administrator), db=Depends(db_session)
    ):
        result = db.execute(
            delete(SystemLog).where(
                SystemLog.owner_id == user.id,
                SystemLog.tenant_id == user.tenant_id,
                SystemLog.created_at < now() - timedelta(days=older_than_days),
            )
        )
        audit(
            db, user, "logs.pruned", user.id, {"count": result.rowcount, "older_than_days": older_than_days}
        )
        db.commit()
        return {"deleted": result.rowcount}

    @app.get("/api/audit")
    def read_audit(page: int = Query(1, ge=1), user=Depends(administrator), db=Depends(db_session)):
        rows = db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.tenant_id == user.tenant_id,
                or_(AuditEvent.actor_id == user.id, ~AuditEvent.action.like("trading.%")),
            )
            .order_by(AuditEvent.created_at.desc())
            .offset((page - 1) * 10)
            .limit(10)
        )
        return [
            {
                "action": row.action,
                "target_id": row.target_id,
                "detail": clean_trading_audit(row.detail)
                if row.action.startswith("trading.")
                else row.detail,
                "created_at": local_time(row.created_at, config.timezone),
            }
            for row in rows
        ]

    @app.get("/api/users")
    def read_users(user=Depends(administrator), db=Depends(db_session)):
        counts = dict(
            db.execute(
                select(AuthSession.user_id, func.count())
                .join(User, User.id == AuthSession.user_id)
                .where(
                    User.tenant_id == user.tenant_id,
                    User.active.is_(True),
                    or_(
                        AuthSession.expires_at > now(),
                        (AuthSession.expires_at.is_(None) & (User.role == "admin")),
                    ),
                    AuthSession.revoked_at.is_(None),
                )
                .group_by(AuthSession.user_id)
            ).all()
        )
        return [
            {**user_view(row), "valid_sessions": counts.get(row.id, 0)}
            for row in db.scalars(
                select(User).where(User.tenant_id == user.tenant_id).order_by(User.created_at)
            )
        ]

    @app.get("/api/admin/sessions")
    def read_session_history(
        request: Request,
        page: int = Query(1, ge=1),
        page_size: int = Query(5),
        status: str = Query("all", pattern="^(all|active|expired|revoked)$"),
        user_id: str | None = Query(None, max_length=36),
        user=Depends(administrator),
        db=Depends(db_session),
    ):
        if page_size not in {5, 10}:
            raise HTTPException(422, "5개 또는 10개씩 표시하세요.")
        instant = now()
        scope = [User.tenant_id == user.tenant_id]
        if user_id:
            scope.append(User.id == user_id)
        if status == "active":
            scope.extend(
                [
                    AuthSession.revoked_at.is_(None),
                    or_(
                        AuthSession.expires_at > instant,
                        (AuthSession.expires_at.is_(None) & (User.role == "admin")),
                    ),
                    User.active.is_(True),
                ]
            )
        elif status == "expired":
            scope.extend(
                [AuthSession.revoked_at.is_(None), AuthSession.expires_at <= instant, User.active.is_(True)]
            )
        elif status == "revoked":
            scope.append(AuthSession.revoked_at.is_not(None) | User.active.is_(False))
        query = select(AuthSession, User).join(User, User.id == AuthSession.user_id).where(*scope)
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        rows = db.execute(
            query.order_by(AuthSession.expires_at.desc(), AuthSession.public_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        current_hash = token_digest(request.cookies[COOKIE])
        items = []
        for session, owner in rows:
            state = (
                "revoked"
                if session.revoked_at or not owner.active
                else "expired"
                if session.expires_at is not None and aware(session.expires_at) <= instant
                else "active"
            )
            items.append(
                {
                    "id": session.public_id,
                    "user_id": owner.id,
                    "email": owner.email,
                    "status": state,
                    "device": session.device,
                    "browser": session.browser,
                    "client_ip": session.client_ip,
                    "created_at": local_time(session.created_at, config.timezone),
                    "last_seen_at": local_time(session.last_seen_at, config.timezone),
                    "metadata_recorded_at": local_time(session.metadata_recorded_at, config.timezone),
                    "expires_at": local_time(session.expires_at, config.timezone),
                    "unlimited": session.expires_at is None and owner.role == "admin",
                    "revoked_at": local_time(session.revoked_at, config.timezone),
                    "end_reason": session.end_reason or ("user_restricted" if not owner.active else None),
                    "is_current": session.token_hash == current_hash,
                }
            )
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @app.post("/api/users", status_code=201)
    def add_user(payload: UserCreate, user=Depends(administrator), db=Depends(db_session)):
        policy = db.scalar(select(AdminSettings).where(AdminSettings.id == 1).with_for_update())
        hours = payload.session_hours or policy.default_session_hours
        if hours > policy.max_session_hours:
            raise HTTPException(422, "서버에서 허용한 최대 세션 시간을 초과했습니다.")
        code = secrets.token_urlsafe(24)
        invited = User(
            tenant_id=user.tenant_id,
            email=str(payload.email).lower(),
            role=payload.role,
            session_hours=hours,
            code_hash=hash_code(code),
        )
        db.add(invited)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "이미 등록된 이메일입니다.") from None
        db.add(UserSettings(user_id=invited.id, tenant_id=user.tenant_id, value=Preferences().model_dump()))
        db.add(
            SchedulePolicy(owner_id=invited.id, tenant_id=user.tenant_id, value=SchedulePlan().model_dump())
        )
        ensure_personal_sources(db, invited)
        audit(db, user, "user.created", invited.id, {"role": payload.role, "session_hours": hours})
        db.commit()
        # Shown once to the administrator. SMTP delivery is a later phase.
        return {
            **user_view(invited),
            **({"access_code": code} if config.legacy_access_code_login_enabled else {}),
        }

    @app.patch("/api/users/{user_id}")
    def change_user(user_id: str, payload: UserUpdate, user=Depends(administrator), db=Depends(db_session)):
        db.scalar(select(Tenant).where(Tenant.id == user.tenant_id).with_for_update())
        policy = db.scalar(select(AdminSettings).where(AdminSettings.id == 1).with_for_update())
        if payload.session_hours is not None and payload.session_hours > policy.max_session_hours:
            raise HTTPException(422, "서버에서 허용한 최대 세션 시간을 초과했습니다.")
        target = db.scalar(select(User).where(User.id == user_id, User.tenant_id == user.tenant_id))
        if not target:
            raise HTTPException(404, "사용자를 찾을 수 없습니다.")
        if target.role == "admin" and target.active and (payload.role != "admin" or not payload.active):
            count = db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.tenant_id == user.tenant_id, User.active.is_(True), User.role == "admin")
            )
            if count <= 1:
                raise HTTPException(409, "마지막 관리자는 제한할 수 없습니다.")
        previous_hours = target.session_hours
        role_changed = target.role != payload.role
        revoke_for_permissions = not payload.active or (role_changed and target.role == "admin")
        duration_changed = payload.session_hours is not None and payload.session_hours != previous_hours
        target.role, target.active = payload.role, payload.active
        if payload.session_hours is not None:
            target.session_hours = payload.session_hours
        if revoke_for_permissions:
            revoke_sessions(
                db,
                AuthSession.user_id == target.id,
                "permissions_changed" if role_changed else "user_restricted",
            )
        elif duration_changed:
            instant = now()
            db.execute(
                update(AuthSession)
                .where(
                    AuthSession.user_id == target.id,
                    AuthSession.revoked_at.is_(None),
                    or_(
                        AuthSession.expires_at > instant,
                        AuthSession.expires_at.is_(None) & (target.role == "admin"),
                    ),
                )
                .values(
                    expires_at=None
                    if target.role == "admin"
                    else instant + timedelta(hours=target.session_hours)
                )
            )
        if duration_changed:
            audit(
                db,
                user,
                "user.session_policy_changed",
                target.id,
                {
                    "previous_hours": previous_hours,
                    "session_hours": target.session_hours,
                    "sessions_revoked": revoke_for_permissions,
                    "applied_to_active_sessions": not revoke_for_permissions,
                },
            )
        audit(db, user, "user.permissions_changed", target.id, payload.model_dump())
        db.commit()
        return user_view(target)

    def event_snapshot(token):
        with factory() as db:
            user = session_user(db, token)
            if not user:
                return None
            rows = db.scalars(
                select(AnalysisRun)
                .where(AnalysisRun.owner_id == user.id, AnalysisRun.tenant_id == user.tenant_id)
                .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id)
                .limit(20)
            )
            return {"runs": [run_view(row, config.timezone) for row in rows]}

    @app.get("/api/events")
    async def events(request: Request, user=Depends(current_user)):
        token = request.cookies.get(COOKIE)

        async def stream():
            previous = None
            ticks = 0
            while not await request.is_disconnected():
                snapshot = await asyncio.to_thread(event_snapshot, token)
                if snapshot is None:
                    yield "event: expired\ndata: {}\n\n"
                    break
                data = json.dumps(snapshot, ensure_ascii=False)
                if data != previous:
                    yield f"data: {data}\n\n"
                    previous = data
                elif ticks % 15 == 0:
                    yield ": keep-alive\n\n"
                ticks += 1
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )

    @app.get("/")
    def index():
        return FileResponse(ROOT / "index.html")

    # Explicit asset allowlist; never expose .env, source, DB files, docs or Git metadata.
    @app.get("/{asset}")
    def asset_file(asset: str):
        if asset not in {
            "app.js",
            "trade-ui.js",
            "live-quotes.js",
            "auto-trade-ui.js",
            "trade-journal-ui.js",
            "reports-ui.js",
            "llm-settings-ui.js",
            "collection-ui.js",
            "data-collection-ui.js",
            "recommendations-ui.js",
            "screen-help.js",
            "styles.css",
            "live.css",
            "manifest.webmanifest",
            "service-worker.js",
            "mobile.css",
        }:
            raise HTTPException(404)
        return FileResponse(ROOT / asset)

    return app
