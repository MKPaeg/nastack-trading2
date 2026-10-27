from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select, update

from .catalog import catalog_changed
from .db import local_time, now
from .instruments import instrument_changed
from .models import AnalysisRun, SystemLog, User, UserSettings
from .security import audit

ACTIVE = ("queued", "running")
TERMINAL = ("completed", "failed")


def run_view(run, timezone_name):
    return {
        "id": run.id,
        "symbol": run.symbol,
        "kind": run.kind,
        "status": run.status,
        "progress": run.progress,
        "message": run.message,
        "result": run.result,
        "error_code": run.error_code,
        "settings_version": run.settings_version,
        "source_catalog_snapshot": run.source_catalog_snapshot,
        "instrument_snapshot": run.instrument_snapshot,
        "attempts": run.attempts,
        "created_at": local_time(run.created_at, timezone_name),
        "started_at": local_time(run.started_at, timezone_name),
        "finished_at": local_time(run.finished_at, timezone_name),
    }


def log(db, user, run_id, message, level="INFO"):
    db.add(SystemLog(tenant_id=user.tenant_id, owner_id=user.id, run_id=run_id, message=message, level=level))


def claim_job(factory, lease_seconds=30):
    """PostgreSQL row locks + conditional update fence concurrent workers."""
    timestamp = now()
    available = or_(
        AnalysisRun.status == "queued",
        and_(AnalysisRun.status == "running", AnalysisRun.lease_until <= timestamp),
    )
    with factory.begin() as db:
        run = db.scalar(
            select(AnalysisRun)
            .where(available)
            .order_by(AnalysisRun.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if run is None:
            return None
        if run.kind == "research" and run.status == "running":
            # A crashed external call may already have incurred cost. Never automatically replay it.
            user = db.get(User, run.owner_id)
            run.status, run.error_code = "failed", "EXTERNAL_CALL_UNKNOWN"
            run.message, run.finished_at, run.lease_until = (
                "외부 호출 중 작업이 중단되었습니다. 수동 재요청하세요.",
                timestamp,
                None,
            )
            audit(db, user, "analysis.failed", run.id, {"error_code": run.error_code})
            log(db, user, run.id, run.message, "ERROR")
            return None
        token = str(uuid4())
        next_attempt = run.attempts + 1
        claimed = db.execute(
            update(AnalysisRun)
            .where(AnalysisRun.id == run.id, available)
            .execution_options(synchronize_session=False)
            .values(
                status="running",
                claim_token=token,
                lease_until=timestamp + timedelta(seconds=lease_seconds),
                started_at=timestamp,
                progress=10,
                message="처리 준비 중",
                attempts=AnalysisRun.attempts + 1,
            )
        )
        if claimed.rowcount != 1:
            return None
        user = db.get(User, run.owner_id)
        # A crash can reclaim only this deterministic demo, never an order or external effect.
        if next_attempt > 3:
            db.execute(
                update(AnalysisRun)
                .where(AnalysisRun.id == run.id)
                .values(
                    status="failed",
                    error_code="RETRY_LIMIT",
                    message="복구 횟수 초과",
                    finished_at=timestamp,
                    lease_until=None,
                )
            )
            audit(db, user, "analysis.failed", run.id, {"error_code": "RETRY_LIMIT"})
            log(db, user, run.id, "복구 횟수 초과", "ERROR")
            return None
        audit(db, user, "analysis.started", run.id)
        log(db, user, run.id, "작업 실행 시작")
        return run.id, token


def transition(
    factory,
    run_id,
    token,
    *,
    progress,
    message,
    status="running",
    result=None,
    error_code=None,
    lease_seconds=30,
):
    with factory.begin() as db:
        run = db.scalar(
            select(AnalysisRun)
            .where(
                AnalysisRun.id == run_id,
                AnalysisRun.claim_token == token,
                AnalysisRun.status == "running",
                AnalysisRun.lease_until > now(),
            )
            .with_for_update()
        )
        if run is None:
            return False
        user = db.get(User, run.owner_id)
        settings = db.get(UserSettings, run.owner_id)
        if not user.active or user.role == "viewer":
            status, error_code, message = "failed", "PERMISSION_CHANGED", "사용 권한이 변경되었습니다."
        elif settings.version != run.settings_version:
            status, error_code, message = "failed", "SETTINGS_CHANGED", "설정 변경으로 재요청이 필요합니다."
        elif catalog_changed(db, run):
            status, error_code, message = (
                "failed",
                "SOURCE_CATALOG_CHANGED",
                "분석 출처 변경으로 재요청이 필요합니다.",
            )
        elif instrument_changed(db, run):
            status, error_code, message = (
                "failed",
                "INSTRUMENT_CHANGED",
                "종목 정보 변경으로 재요청이 필요합니다.",
            )
        run.status, run.progress, run.message = status, progress, message
        run.result = result if status == "completed" else None
        run.error_code = error_code
        run.lease_until = now() + timedelta(seconds=lease_seconds) if status == "running" else None
        if status in TERMINAL:
            run.finished_at = now()
            audit(db, user, f"analysis.{status}", run.id, {"error_code": error_code})
        log(db, user, run.id, message, "ERROR" if status == "failed" else "INFO")
        return status == "running" or status == "completed"


def demo_result(run):
    """Pipeline verification only. No inferred financial facts, recommendations or external calls."""
    return {
        "simulation": True,
        "symbol": run.symbol,
        "action": "abstain",
        "evidence": [],
        "summary": "작업 관리·저장 흐름 검증을 완료했습니다. 실제 종목 분석은 아직 연결되지 않았습니다.",
        "model": run.settings_snapshot["analysis_model"],
        "source_count": len(run.settings_snapshot["sources"]),
        "settings_version": run.settings_version,
    }
