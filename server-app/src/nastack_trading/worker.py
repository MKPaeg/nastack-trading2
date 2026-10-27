import argparse
import time
from datetime import timedelta
from threading import Event, Thread

from sqlalchemy import select, update

from .config import Config
from .db import connect, now
from .jobs import claim_job, demo_result, transition
from .models import AnalysisRun
from .research import ResearchError, ResearchPipeline


def work_once(factory, lease_seconds=30, pause=time.sleep, evaluator=demo_result):
    claimed = claim_job(factory, lease_seconds)
    if not claimed:
        return False
    run_id, token = claimed
    stop = Event()

    def heartbeat():
        while not stop.wait(lease_seconds / 3):
            with factory.begin() as db:
                db.execute(
                    update(AnalysisRun)
                    .where(
                        AnalysisRun.id == run_id,
                        AnalysisRun.claim_token == token,
                        AnalysisRun.status == "running",
                        AnalysisRun.lease_until > now(),
                    )
                    .values(lease_until=now() + timedelta(seconds=lease_seconds))
                )

    thread = Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        with factory() as db:
            run = db.scalar(select(AnalysisRun).where(AnalysisRun.id == run_id))
            db.expunge(run)
        stages = (
            ((20, "설정 snapshot 확인 중"),)
            if run.kind == "research"
            else ((35, "설정 snapshot 확인 중"), (70, "결과 저장 준비 중"))
        )
        for progress, message in stages:
            pause(0.8)
            if not transition(
                factory, run_id, token, progress=progress, message=message, lease_seconds=lease_seconds
            ):
                return True
        if isinstance(evaluator, ResearchPipeline) and run.kind == "research":
            from .models import User
            from .personal_connections import external_config

            with factory() as db:
                owner = db.get(User, run.owner_id)
                if not owner or not owner.active:
                    raise ResearchError("RESEARCH_DISABLED")
                personal_evaluator = ResearchPipeline(
                    external_config(db, owner, evaluator.config), evaluator.transport,
                    prompt=run.settings_snapshot.get("_research_prompt"),
                )
            result = personal_evaluator(
                run,
                lambda progress, message: transition(
                    factory, run_id, token, progress=progress, message=message, lease_seconds=lease_seconds
                ),
            )
        else:
            if run.kind == "research":
                raise ResearchError("RESEARCH_DISABLED")
            result = demo_result(run) if isinstance(evaluator, ResearchPipeline) else evaluator(run)
        transition(
            factory,
            run_id,
            token,
            progress=100,
            message="근거 분석 완료 · 주문 미연결" if run.kind == "research" else "검증 작업 완료",
            status="completed",
            result=result,
            lease_seconds=lease_seconds,
        )
    except Exception as error:
        # Never persist raw provider exceptions, which may contain secrets or document contents.
        transition(
            factory,
            run_id,
            token,
            progress=0,
            message="작업 처리 실패. 다시 요청하세요.",
            status="failed",
            error_code=error.code if isinstance(error, ResearchError) else "PROCESSING_FAILED",
            lease_seconds=lease_seconds,
        )
    finally:
        stop.set()
        thread.join(timeout=2)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = Config()
    engine, factory = connect(config.database_url)
    try:
        while True:
            worked = work_once(factory, config.worker_lease_seconds, evaluator=ResearchPipeline(config))
            if args.once:
                break
            if not worked:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
